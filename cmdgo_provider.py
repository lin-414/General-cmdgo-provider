#!/usr/bin/env python3
"""
cmdgo-provider — 纯 Python 的本地 OpenAI 兼容适配器，把 Command Code "Go" 套餐接入 Hermes Studio。

背景
----
Command Code Go 套餐**没有** OpenAI 兼容端点：标准 /v1/chat/completions 对 Go 订阅返回
403，所有请求都必须走私有 CLI 网关 ``POST /alpha/generate``（自定义信封 + NDJSON 流）。

本项目内含一个**零依赖**（仅标准库）的 OpenAI 兼容代理服务器，把 OpenAI 的
chat/completions 请求翻译成 Command Code 网关信封，并把网关的 NDJSON 事件流还原成
OpenAI 的 SSE 流。推荐以独立进程运行：``python run.py`` 直接在前台启动代理，或使用
Windows 启动脚本后台运行。

HTTP 接口（与 OpenAI 兼容，可直接填进 Hermes Studio 的「自定义 OpenAI 兼容供应商」）
  POST /v1/chat/completions   流式(SSE) + 非流式，含 reasoning_content / 工具调用 / usage
  GET  /v1/models             返回 Go 套餐模型目录
  POST /login  +  GET /login/status   OAuth 登录（Go 套餐没有静态 API Key，走账号授权）
  POST /login/cancel           取消登录
  POST /logout                 清除已缓存的 key
  GET  /healthz

鉴权说明（重要）
  Command Code Go 套餐**不发放静态 API Key**。本适配器通过 OAuth 登录拿到 Bearer token，
  登录成功后**自动缓存**（内存 + token.json 落盘），之后所有 chat 请求自动使用该 key。
  因此 Hermes Studio 里填的供应商 Key 可以是任意占位符（如 "cmdgo"），代理会用缓存的
  Go key 覆盖它。登录一次即可，重启后从 token.json 自动恢复。

环境变量
  PORT                  监听端口（默认 8787）
  COMMANDCODE_BASE_URL  网关基址（默认 https://api.commandcode.ai）
  COMMANDCODE_API_KEY   若设置，则覆盖 OAuth 缓存的 key（仍可用，但不是 Go 的常规方式）
  API_KEY               同上（兼容别名）
  CC_VERSION            伪装 CLI 版本（默认 1.31.0）
  CC_PROJECT_SLUG       x-project-slug（默认 dsh-cmdgo）
  LOGIN_STUDIO_BASE     OAuth studio 来源（默认 https://commandcode.ai）
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.client
import json
import logging
import os
import platform
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 配置（来自环境变量，运行时可被 run.py 的命令行参数覆盖）
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8787"))
BASE_URL = (os.environ.get("COMMANDCODE_BASE_URL", "https://api.commandcode.ai")).rstrip("/")
CC_VERSION = os.environ.get("CC_VERSION", "1.31.0")
PROJECT_SLUG = os.environ.get("CC_PROJECT_SLUG", "dsh-cmdgo")
STUDIO_BASE = (os.environ.get("LOGIN_STUDIO_BASE", "https://commandcode.ai")).rstrip("/")
OVERRIDE_KEY = os.environ.get("COMMANDCODE_API_KEY") or os.environ.get("API_KEY", "")
DEFAULT_MAX_TOKENS = 64000
MODELS_URL = "https://api.commandcode.ai/provider/v1/models"
MODELS_REFRESH_S = 15 * 60

logging.basicConfig(level=logging.INFO, format="[cmdgo-provider %(asctime)s] %(message)s")
log = logging.getLogger("cmdgo-provider").info

# ---------------------------------------------------------------------------
# API Key 缓存（Go 套餐没有静态 Key，走账号 OAuth；登录后缓存 Bearer token）
# ---------------------------------------------------------------------------
def _data_dir() -> str:
    """Return a writable per-user data directory."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(root, "cmdgo-provider")
    os.makedirs(path, exist_ok=True)
    return path


TOKEN_FILE = os.path.join(_data_dir(), "token.json")
cached_api_key = ""


def _load_cached_key() -> None:
    global cached_api_key
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        k = d.get("apiKey")
        if isinstance(k, str) and k:
            cached_api_key = k
            log("已从 %s 载入缓存的 apiKey", TOKEN_FILE)
    except Exception:
        pass


def _save_cached_key(key: str) -> None:
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"apiKey": key}, f)
    except Exception as e:
        log("无法持久化 apiKey: %s", e)


_load_cached_key()

# ---------------------------------------------------------------------------
# Go 套餐模型过滤（移植自 dsh-cmdgo-provider/src/models.ts）
# ---------------------------------------------------------------------------
GO_PREMIUM_EXCEPTIONS = {"gpt-5.6-luna", "xai/grok-4.5", "meta/muse-spark-1.2-contributor"}
PREMIUM_ONLY_PREFIXES = ("google/", "sakana/", "anthropic/")
PREMIUM_BRANDS = ("claude-", "gpt-", "gemini-", "grok-", "fugu-", "muse-spark")


def is_go_model(_id: str) -> bool:
    if _id in GO_PREMIUM_EXCEPTIONS:
        return True
    if any(_id.startswith(p) for p in PREMIUM_ONLY_PREFIXES):
        return False
    short = _id.split("/", 1)[1] if "/" in _id else _id
    if any(short.startswith(b) for b in PREMIUM_BRANDS):
        return False
    return True


model_cache: dict = {"models": [{"id": x, "name": x} for x in GO_PREMIUM_EXCEPTIONS], "at": 0.0}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def extract_bearer(h) -> str:
    if isinstance(h, str) and h.startswith("Bearer "):
        return h[7:].strip()
    return ""


def build_session_id() -> str:
    return "cli-" + datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def fingerprint_headers(api_key: str) -> dict:
    return {
        "content-type": "application/json",
        "user-agent": f"commandcode/{CC_VERSION}",
        "x-command-code-version": CC_VERSION,
        "x-cli-environment": "production",
        "x-taste-learning": "false",
        "x-session-id": build_session_id(),
        "x-project-slug": PROJECT_SLUG,
        "authorization": f"Bearer {api_key}",
    }


def gateway_error_message(body: str):
    try:
        p = json.loads(body)
        if isinstance(p, dict):
            e = p.get("error")
            if isinstance(e, dict) and isinstance(e.get("message"), str) and e["message"]:
                return e["message"]
    except Exception:
        pass
    return None


def error_type(status: int) -> str:
    if status in (401, 403):
        return "authentication_error"
    if status == 429:
        return "rate_limit_exceeded"
    if status == 400:
        return "invalid_request_error"
    if status >= 500:
        return "server_error"
    return "api_error"


# ---------------------------------------------------------------------------
# OpenAI 请求 -> Command Code /alpha/generate 信封
# ---------------------------------------------------------------------------
def openai_to_cc_envelope(body: dict) -> dict:
    system = ""
    messages = []
    for m in body.get("messages", []) or []:
        role = m.get("role")
        if role == "system":
            system += (("\n\n" if system else "") + flatten_text(m.get("content", "")))
            continue
        if role == "assistant":
            content = []
            rc = m.get("reasoning_content")
            if isinstance(rc, str) and rc:
                content.append({"type": "reasoning", "text": rc})
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") == "text":
                        content.append({"type": "text", "text": b.get("text", "")})
                    elif b.get("type") == "reasoning":
                        content.append({"type": "reasoning", "text": b.get("text", "")})
            elif isinstance(c, str) and c:
                content.append({"type": "text", "text": c})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                try:
                    args = json.loads(fn.get("arguments", "{}") or "{}")
                except Exception:
                    args = {}
                content.append({
                    "type": "tool-call",
                    "toolCallId": tc.get("id"),
                    "toolName": fn.get("name"),
                    "input": args,
                })
            messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            messages.append({
                "role": "tool",
                "content": [{
                    "type": "tool-result",
                    "toolCallId": m.get("tool_call_id"),
                    "toolName": "unknown",
                    "output": {
                        "type": "text",
                        "value": m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content")),
                    },
                }],
            })
        else:  # user
            text = flatten_text(m.get("content", ""))
            if text:
                messages.append({"role": "user", "content": text})
            elif isinstance(m.get("content"), list):
                tool_results = []
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        tool_results.append({
                            "type": "tool-result",
                            "toolCallId": b.get("tool_call_id"),
                            "toolName": "unknown",
                            "output": {
                                "type": "text",
                                "value": b.get("content") if isinstance(b.get("content"), str) else json.dumps(b.get("content")),
                            },
                        })
                if tool_results:
                    messages.append({"role": "tool", "content": tool_results})

    tools = []
    for t in body.get("tools", []) or []:
        fn = t.get("function", {}) or {}
        item = {"type": "function", "name": fn.get("name")}
        if fn.get("description"):
            item["description"] = fn["description"]
        item["input_schema"] = fn.get("parameters", {}) or {}
        tools.append(item)

    params = {
        "model": body.get("model"),
        "messages": messages,
        "tools": tools,
        "system": system,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or DEFAULT_MAX_TOKENS,
        "stream": True,
    }
    re_effort = body.get("reasoning_effort") or body.get("reasoningEffort") or (body.get("reasoning") or {}).get("effort")
    if re_effort and re_effort != "off":
        params["reasoning_effort"] = re_effort
    if body.get("temperature") is not None:
        params["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        params["top_p"] = body["top_p"]

    return {
        "config": {
            "workingDir": os.getcwd(),
            "date": datetime.date.today().isoformat(),
            "environment": f"{platform.system()}-{platform.machine()}",
            "structure": [],
            "isGitRepo": False,
            "currentBranch": "",
            "mainBranch": "",
            "gitStatus": "",
            "recentCommits": [],
        },
        "memory": "",
        "taste": "",
        "skills": None,
        "permissionMode": "standard",
        "params": params,
    }


# ---------------------------------------------------------------------------
# Command Code 流事件 -> OpenAI SSE payload
# ---------------------------------------------------------------------------
def map_finish_reason(raw) -> str:
    r = raw if isinstance(raw, str) else "stop"
    if r in ("stop", "end_turn"):
        return "stop"
    if r in ("tool_calls", "tool-calls"):
        return "tool_calls"
    if r in ("length", "max_tokens", "max-output-tokens"):
        return "length"
    return "stop"


def event_to_openai(event: dict, state: dict):
    out = []
    t = event.get("type")
    if t == "text-delta":
        text = event.get("text", "")
        if text:
            out.append({"choices": [{"index": 0, "delta": {"content": text}}]})
    elif t == "reasoning-delta":
        text = event.get("text", "")
        if text:
            out.append({"choices": [{"index": 0, "delta": {"reasoning_content": text}}]})
    elif t == "tool-call":
        inp = event.get("input") or event.get("args") or event.get("arguments")
        call_id = event.get("toolCallId") or event.get("id") or f"call_{state['toolIdx']}"
        name = event.get("toolName", "") or ""
        args_str = inp if isinstance(inp, str) else json.dumps(inp if inp is not None else {})
        out.append({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": state["toolIdx"],
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                }]},
            }],
        })
        state["toolIdx"] += 1
    elif t == "finish-step":
        usage = event.get("usage")
        if isinstance(usage, dict) and (state.get("wantUsage") or True):
            out.append({
                "choices": [],
                "usage": {
                    "prompt_tokens": usage.get("inputTokens", 0),
                    "completion_tokens": usage.get("outputTokens", 0),
                    "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
                },
            })
        out.append({
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": map_finish_reason(event.get("finishReason") or event.get("rawFinishReason")),
            }],
        })
    return out


def build_nonstream(model: str, events: list, created: int) -> dict:
    text = ""
    reasoning = ""
    tool_calls = []
    finish_reason = "stop"
    usage = None
    for ev in events:
        if ev.get("type") == "text-delta":
            text += ev.get("text", "")
        elif ev.get("type") == "reasoning-delta":
            reasoning += ev.get("text", "")
        elif ev.get("type") == "tool-call":
            inp = ev.get("input") or ev.get("args") or ev.get("arguments")
            args_str = inp if isinstance(inp, str) else json.dumps(inp if inp is not None else {})
            tool_calls.append({
                "id": ev.get("toolCallId") or ev.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {"name": ev.get("toolName", "") or "", "arguments": args_str},
            })
        elif ev.get("type") == "finish-step":
            if isinstance(ev.get("usage"), dict):
                u = ev["usage"]
                usage = {
                    "prompt_tokens": u.get("inputTokens", 0),
                    "completion_tokens": u.get("outputTokens", 0),
                    "total_tokens": u.get("inputTokens", 0) + u.get("outputTokens", 0),
                }
            finish_reason = map_finish_reason(ev.get("finishReason") or ev.get("rawFinishReason"))
    message = {"role": "assistant", "content": text or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------------------
# 模型目录刷新
# ---------------------------------------------------------------------------
def refresh_models() -> None:
    global model_cache
    try:
        # Cloudflare rejects urllib's default ``Python-urllib/...`` user-agent
        # with HTTP 403 even though this public catalog is unauthenticated.
        # Use the same CLI fingerprint as gateway requests, but do not attach
        # the Go OAuth token: this is the public Provider catalog endpoint.
        req = urllib.request.Request(MODELS_URL, headers={
            "accept": "application/json",
            "user-agent": f"commandcode/{CC_VERSION}",
            "x-command-code-version": CC_VERSION,
            "x-cli-environment": "production",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            p = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(p, dict) or not isinstance(p.get("data"), list):
            raise ValueError("unexpected shape")
        models = []
        for m in p["data"]:
            if isinstance(m, dict) and isinstance(m.get("id"), str) and is_go_model(m["id"]):
                name = m["name"] if isinstance(m.get("name"), str) else m["id"].split("/")[-1]
                models.append({"id": m["id"], "name": name})
        models.sort(key=lambda x: x["id"])
        model_cache = {"models": models, "at": time.time()}
        log("synced %d Go models", len(models))
    except Exception as e:
        log("model refresh failed: %s", e)


def model_refresh_loop() -> None:
    while True:
        refresh_models()
        time.sleep(MODELS_REFRESH_S)


# ---------------------------------------------------------------------------
# OAuth 登录（移植自 dsh-cmdgo-provider/src/oauth.ts）—— 一次性回调监听
# ---------------------------------------------------------------------------
class LoginManager:
    def __init__(self) -> None:
        self.server = None
        self.port = None
        self.expected_state = None
        self.auth_url = None
        self.callback_url = None
        self.started_at = 0
        self.last_result = {"status": "idle"}
        self._event = threading.Event()
        self._result = None
        self._lock = threading.RLock()

    def status(self) -> dict:
        if self.server is not None and self.auth_url is not None:
            return {
                "status": "waiting",
                "authUrl": self.auth_url,
                "callbackUrl": self.callback_url,
                "startedAt": self.started_at,
                "hasKey": bool(cached_api_key),
            }
        result = dict(self.last_result)
        result["hasKey"] = bool(cached_api_key)
        return result

    def start(self) -> dict:
        with self._lock:
            if self.server is not None and self.auth_url is not None:
                return {"authUrl": self.auth_url, "callbackUrl": self.callback_url}
            self.stop("new login requested")
            self.port = self._bind()
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
            self.expected_state = secrets.token_urlsafe(32)
            self.started_at = int(time.time() * 1000)
            self.callback_url = f"http://localhost:{self.port}/callback"
            self.auth_url = (
                f"{STUDIO_BASE}/studio/auth/cli"
                f"?callback={urllib.parse.quote(self.callback_url, safe='')}"
                f"&state={urllib.parse.quote(self.expected_state, safe='')}"
            )
            self._event = threading.Event()
            self._result = None
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            log("waiting for Command Code callback: %s", self.callback_url)
            return {"authUrl": self.auth_url, "callbackUrl": self.callback_url}

    def wait_for_callback(self, timeout: float = 600):
        self._event.wait(timeout)
        return self._result

    def stop(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self.server is not None:
                try:
                    self.server.shutdown()
                    self.server.server_close()
                except Exception:
                    pass
                self.server = None
            self.expected_state = None
            self.auth_url = None
            self.callback_url = None

    def _bind(self) -> int:
        for p in range(5959, 5959 + 10):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", p))
                s.close()
                return p
            except OSError:
                continue
        raise RuntimeError("无法启动回调服务器：端口 5959..5968 均被占用")

    def _make_handler(self):
        mgr = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", STUDIO_BASE)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")

            def do_OPTIONS(self):
                self.send_response_only(204)
                self._cors()
                self.end_headers()

            def do_POST(self):
                if self.path.split("?")[0] != "/callback":
                    self.send_response_only(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":false,"error":"not found"}')
                    return
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("error"):
                    self.send_response_only(200)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":true}')
                    mgr._finish({"status": "error", "message": "授权被取消或失败：" + q["error"][0], "at": int(time.time() * 1000)})
                    return
                try:
                    length = int(self.headers.get("content-length", 0))
                    raw = self.rfile.read(length) if length else b""
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    self.send_response_only(400)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"success":false,"error":"invalid JSON"}')
                    return
                api_key = payload.get("apiKey") if isinstance(payload, dict) else None
                state = payload.get("state") if isinstance(payload, dict) else None
                if not isinstance(api_key, str) or not isinstance(state, str):
                    self.send_response_only(400)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write('{"success":false,"error":"缺少 apiKey 或 state"}'.encode("utf-8"))
                    return
                if mgr.expected_state is None or state != mgr.expected_state:
                    self.send_response_only(400)
                    self._cors()
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write('{"success":false,"error":"state 不匹配"}'.encode("utf-8"))
                    return
                self.send_response_only(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success":true}')
                info = {
                    "apiKey": api_key,
                    "userId": payload.get("userId"),
                    "userName": payload.get("userName"),
                    "keyName": payload.get("keyName"),
                }
                log("授权成功：user=%s key=%s", info.get("userName"), info.get("keyName"))
                mgr._finish({"status": "success", "userName": info.get("userName"),
                             "keyName": info.get("keyName"), "apiKey": info.get("apiKey"),
                             "at": int(time.time() * 1000)}, info)

        return _H

    def _finish(self, result: dict, info=None) -> None:
        with self._lock:
            self.last_result = result
            self._result = info if (info is not None and result.get("status") == "success") else result
            # 登录成功后缓存 Go 套餐的 Bearer token（OAuth 拿到的，无静态 key）
            if info is not None and result.get("status") == "success" and isinstance(info.get("apiKey"), str):
                global cached_api_key
                cached_api_key = info["apiKey"]
                _save_cached_key(cached_api_key)
                log("已缓存 CommandCode apiKey（Go 套餐），后续请求自动使用")
            if self.server is not None:
                try:
                    self.server.shutdown()
                    self.server.server_close()
                except Exception:
                    pass
                self.server = None
            self.expected_state = None
            self.auth_url = None
            self.callback_url = None
            self._event.set()


login = LoginManager()


# ---------------------------------------------------------------------------
# HTTP _helper
# ---------------------------------------------------------------------------
def send_json(handler, status: int, obj) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def sse(handler, obj) -> None:
    if isinstance(obj, str):
        handler.wfile.write(("data: " + obj + "\n\n").encode("utf-8"))
    else:
        handler.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
    handler.wfile.flush()


def handle_models(handler) -> None:
    data = [{"id": m["id"], "object": "model", "created": 0,
             "owned_by": "commandcode-go", "name": m["name"]} for m in model_cache["models"]]
    send_json(handler, 200, {"object": "list", "data": data})


def iter_ndjson_lines(resp):
    """逐行读取 NDJSON（对 chunked 响应也能增量产出事件）。"""
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8", "replace").strip()
        if s and not s.startswith(":"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                    yield parsed
            except Exception:
                pass


def handle_chat(handler, body: dict) -> None:
    # 优先级：显式环境变量 > OAuth 登录缓存 > 请求里的 Authorization。
    # Studio 必须发送一个 API Key 字段，但 Go 套餐没有静态 Key；用户填的
    # "cmdgo" 等占位符不能覆盖真正的 OAuth token。只有在没有缓存时，才
    # 允许直接使用请求里的 Bearer 值（便于手工调用/兼容其他客户端）。
    api_key = OVERRIDE_KEY or cached_api_key or extract_bearer(handler.headers.get("authorization", ""))
    if not api_key:
        send_json(handler, 401, {"error": {"message": "尚未登录 CommandCode：Go 套餐没有静态 API Key，请先 `POST /login` 在浏览器完成账号 OAuth 授权（授权一次即可，key 会自动缓存）。", "type": "invalid_request_error"}})
        return

    envelope = openai_to_cc_envelope(body)
    headers = fingerprint_headers(api_key)
    created = int(time.time())
    model = body.get("model")
    want_stream = body.get("stream") is True
    include_usage = want_stream and isinstance(body.get("stream_options"), dict) and body["stream_options"].get("include_usage") is True
    log("chat: stream=%s model=%s", want_stream, model)

    data = json.dumps(envelope).encode("utf-8")
    conn = None
    try:
        conn, upstream = _open_stream(BASE_URL + "/alpha/generate", data, headers)
        log("chat: upstream status=%s", upstream.status)
    except Exception as e:
        log("chat: connect threw: %s", e)
        send_json(handler, 502, {"error": {"message": f"Command Code gateway unreachable: {e}", "type": "server_error"}})
        return

    try:
        if upstream.status >= 400:
            raw = _safe_read(upstream)
            msg = gateway_error_message(raw) or f"Command Code API error (HTTP {upstream.status})"
            st = 401 if upstream.status in (401, 403) else 429 if upstream.status == 429 else 502 if upstream.status >= 500 else 400
            send_json(handler, st, {"error": {"message": msg, "type": error_type(upstream.status)}})
            return

        if not want_stream:
            events = list(iter_ndjson_lines(upstream))
            resp = build_nonstream(model, events, created)
            send_json(handler, 200, resp)
            return

        # streaming
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.flush()
        sse(handler, {"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
        state = {"toolIdx": 0, "wantUsage": include_usage}
        try:
            for ev in iter_ndjson_lines(upstream):
                for p in event_to_openai(ev, state):
                    sse(handler, p)
                if ev.get("type") == "finish-step":
                    break
        except Exception as e:
            sse(handler, {"error": {"message": f"stream error: {e}"}})
        sse(handler, "[DONE]")
        handler.wfile.flush()
    finally:
        try:
            upstream.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _open_stream(url: str, data: bytes, headers: dict):
    """用 http.client 直接打开上游（支持 chunked NDJSON 的增量 readline）。"""
    parsed = urllib.parse.urlparse(url)
    is_https = parsed.scheme == "https"
    host = parsed.hostname
    port = parsed.port or (443 if is_https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    h = {k: ("" if v is None else str(v)) for k, v in headers.items()}
    if is_https:
        conn = http.client.HTTPSConnection(host, port, timeout=600)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=600)
    conn.request("POST", path, body=data, headers=h)
    return conn, conn.getresponse()


def _safe_read(resp) -> str:
    try:
        return resp.read().decode("utf-8", "replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 主代理 HTTP 处理
# ---------------------------------------------------------------------------
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _options(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_OPTIONS(self):
        self._options()

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        if p == "/v1/models":
            handle_models(self)
            return
        if p == "/login/status":
            send_json(self, 200, login.status())
            return
        if p in ("", "/healthz"):
            send_json(self, 200, {"ok": True, "service": "cmdgo-hermes-provider",
                                  "models": len(model_cache["models"]), "baseUrl": BASE_URL})
            return
        send_json(self, 404, {"error": {"not found": True, "hint": "try /v1/chat/completions, /v1/models, /login, /healthz"}})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        try:
            if p == "/v1/chat/completions":
                length = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    send_json(self, 400, {"error": {"message": "invalid JSON body", "type": "invalid_request_error"}})
                    return
                handle_chat(self, body)
                return
            if p == "/login":
                r = login.start()
                send_json(self, 200, {"ok": True, **r})
                return
            if p == "/login/cancel":
                login.stop("user cancel")
                send_json(self, 200, {"ok": True})
                return
            if p == "/logout":
                global cached_api_key
                cached_api_key = ""
                try:
                    os.remove(TOKEN_FILE)
                except Exception:
                    pass
                send_json(self, 200, {"ok": True})
                return
        except Exception as e:
            send_json(self, 500, {"error": {"message": str(e), "type": "api_error"}})
            return
        send_json(self, 404, {"error": {"not found": True}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
_server = None


def make_server():
    return _Server(("127.0.0.1", PORT), ProxyHandler)


def start_server(block: bool = False):
    global _server
    if _server is not None:
        log("server already running on port %s", PORT)
        return _server
    _server = make_server()
    threading.Thread(target=model_refresh_loop, daemon=True).start()
    if block:
        log("listening on http://localhost:%s  (COMMANDCODE_BASE_URL=%s, CLI %s)", PORT, BASE_URL, CC_VERSION)
        if OVERRIDE_KEY:
            log("COMMANDCODE_API_KEY env set: incoming Authorization headers are ignored.")
        _server.serve_forever()
    else:
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        log("listening on http://localhost:%s (background)", PORT)
    return _server


def main():
    global PORT, BASE_URL, OVERRIDE_KEY, CC_VERSION
    ap = argparse.ArgumentParser(description="CommandCode Go -> OpenAI 兼容代理 (纯 Python)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--api-key", default=OVERRIDE_KEY)
    ap.add_argument("--cc-version", default=CC_VERSION)
    args = ap.parse_args()
    PORT = args.port
    BASE_URL = args.base_url.rstrip("/")
    if args.api_key:
        OVERRIDE_KEY = args.api_key
    CC_VERSION = args.cc_version
    start_server(block=True)


if __name__ == "__main__":
    main()
