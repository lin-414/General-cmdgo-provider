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
# ---------------------------------------------------------------------------
# 项目内模块导入（pool 与本文件共用同一个数据目录；先保证项目根目录在 sys.path，
# 无论按脚本、按 run.py 还是被测试按文件加载都能找到）
# ---------------------------------------------------------------------------
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERE_DIR not in sys.path:
    sys.path.insert(0, _HERE_DIR)
from pool import AccountPool, POOL_FILE, data_dir as _data_dir  # noqa: E402


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


# CLI 网关的 x-session-id 语义是「每个 CLI 会话一个」；进程内保持不变，
# 若每个请求都换新值，上游看到的会话数会被放大。
SESSION_ID = "cli-" + datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def build_session_id() -> str:
    return SESSION_ID


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


# ---------------------------------------------------------------------------
# OpenAI 请求 -> Command Code /alpha/generate 信封
# ---------------------------------------------------------------------------
def openai_to_cc_envelope(body: dict) -> dict:
    system = ""
    messages = []
    tool_name_map = {}  # tool_call_id -> toolName, populated from assistant messages
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
                if tc.get("id"):
                    tool_name_map[tc["id"]] = fn.get("name")
            messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            messages.append({
                "role": "tool",
                "content": [{
                    "type": "tool-result",
                    "toolCallId": m.get("tool_call_id"),
                    "toolName": tool_name_map.get(m.get("tool_call_id"), "unknown"),
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
                            "toolName": tool_name_map.get(b.get("tool_call_id"), "unknown"),
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


def event_error_message(event: dict) -> str:
    msg = event.get("message")
    if isinstance(msg, str) and msg:
        return msg
    err = event.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
        return err["message"]
    if isinstance(err, str) and err:
        return err
    return "unknown Command Code stream error"


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
        if isinstance(usage, dict) and state.get("wantUsage"):
            out.append({
                "choices": [],
                "usage": {
                    "prompt_tokens": usage.get("inputTokens", 0),
                    "completion_tokens": usage.get("outputTokens", 0),
                    "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
                },
            })
        # finish_reason 暂存在 state 里，由 handle_chat 在整条流结束时统一发一次。
        # 多步工具流会送来多个 finish-step，而 OpenAI 协议里 finish_reason 是终态，
        # 中间步骤就发出去会让部分客户端提前收尾、丢掉后续步骤的内容。
        state["finishReason"] = map_finish_reason(event.get("finishReason") or event.get("rawFinishReason"))
    elif t == "error":
        # 网关在 200 流中回 error 事件：转发给客户端，而不是静默吞掉返回空回复。
        msg = event_error_message(event)
        state["sawError"] = True
        state["errorMessage"] = msg
        out.append({"error": {"message": msg, "type": "api_error"}})
    return out


def build_nonstream(model: str, events: list, created: int) -> dict:
    text = ""
    reasoning = ""
    tool_calls = []
    finish_reason = "stop"
    usage = None
    for ev in events:
        if ev.get("type") == "error":
            # 网关在 200 响应里夹带 error 事件：抛出让调用方回 502，而不是返回空成功。
            raise ValueError(event_error_message(ev))
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
        # 状态查询是给面板/轮询用的，绝不回传明文 apiKey（历史上登出后仍会残留在响应里）。
        result.pop("apiKey", None)
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
            self.callback_url = f"http://127.0.0.1:{self.port}/callback"
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
                # 通知等待者登录已取消：轮询方（GUI/Web/login.py）读到 cancelled 即刻退出，
                # 不必空等到 10 分钟超时。
                self.last_result = {"status": "cancelled", "message": reason, "at": int(time.time() * 1000)}
                self._result = None
                self._event.set()
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
                # 多号池：把这次登录的 key 入池（已存在则刷新标签）。
                persist_login_key(info)
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
# 多账号池（移植自上游 dsh-cmdgo-provider/src/pool.ts）
# ---------------------------------------------------------------------------
# 账号池：key 与元数据存于 %APPDATA%\cmdgo-provider\accounts.json。
pool = AccountPool("cmdgo", log)
pool.adopt_legacy()


def pool_status() -> dict:
    """给客户端/面板用的账号池状态快照（绝不携带明文 key）。"""
    return {
        "size": pool.size,
        "activeCount": pool.active_count(),
        "accounts": pool.describe_all(),
    }


def persist_login_key(info: dict) -> None:
    """登录成功后：key 已入池则只刷新标签，否则新增账号入池。"""
    api_key = info.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        return
    known = pool.find_by_key(api_key)
    if known is not None:
        if info.get("userName") is not None:
            known.userName = info["userName"]
        if info.get("keyName") is not None:
            known.keyName = info["keyName"]
        log("该 key 已在账号池（%s），仅刷新标签", known.id)
        pool._persist()
        return
    pool.add(info)
    log("API key 已入池，共 %d 个账号", pool.size)


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


def _chat_candidates(handler) -> list:
    """按优先级生成 (api_key, account) 候选列表：
    环境变量覆盖 > 账号池（全部启用账号，轮询顺序）> OAuth 登录缓存 > 请求头 Bearer。
    Studio 必须发送一个 API Key 字段，但 Go 套餐没有静态 Key；用户填的
    "cmdgo" 等占位符不能覆盖真正的 OAuth token。"""
    if OVERRIDE_KEY:
        return [(OVERRIDE_KEY, None)]
    if pool.size > 0:
        first = pool.pick()
        if first is None:
            # 池非空但全部停用：回落到缓存/请求头，而不是直接报未登录。
            key = cached_api_key or extract_bearer(handler.headers.get("authorization", ""))
            return [(key, None)] if key else []
        seen = {first.id}
        out = [(first.apiKey, first)]
        for _ in range(pool.size - 1):
            nxt = pool.pick()
            if nxt is None or nxt.id in seen:
                break  # 已无新的启用账号（全部冷却时 pick 会重复同一账号）
            seen.add(nxt.id)
            out.append((nxt.apiKey, nxt))
        return [(k, a) for k, a in out if k]
    key = cached_api_key or extract_bearer(handler.headers.get("authorization", ""))
    return [(key, None)] if key else []


def _attempt_chat(handler, body: dict, api_key: str, account, created: int):
    """发起一次上游请求并完整处理响应。

    返回 (outcome, failure)：
      ok         成功响应已完整发给客户端
      stream_err 流式响应已开始后出错（已向客户端发送错误，不可换号重试）
      error      尚未向客户端发送任何字节；failure=(status, message, err_type, retryable)
    """
    envelope = openai_to_cc_envelope(body)
    headers = fingerprint_headers(api_key)
    model = body.get("model")
    want_stream = body.get("stream") is True
    include_usage = want_stream and isinstance(body.get("stream_options"), dict) and body["stream_options"].get("include_usage") is True
    log("chat: attempt model=%s account=%s stream=%s", model, account.id if account else "-", want_stream)

    data = json.dumps(envelope).encode("utf-8")
    conn = None
    try:
        conn, upstream = _open_stream(BASE_URL + "/alpha/generate", data, headers)
        log("chat: upstream status=%s", upstream.status)
    except Exception as e:
        log("chat: connect threw: %s", e)
        if account is not None:
            pool.report_failure(account, f"connect: {e}")
        return "error", (502, f"Command Code gateway unreachable: {e}", "server_error", True)

    try:
        if upstream.status >= 400:
            raw = _safe_read(upstream)
            msg = gateway_error_message(raw) or f"Command Code API error (HTTP {upstream.status})"
            # 网关把「模型不存在」也回 401；改写成 400，避免客户端误判为鉴权故障。
            if upstream.status == 401 and "not recognized" in msg.lower():
                return "error", (400, msg, "invalid_request_error", False)
            if upstream.status in (401, 403):
                st, et, retry = 401, "authentication_error", True
            elif upstream.status == 429:
                st, et, retry = 429, "rate_limit_exceeded", True
            elif upstream.status >= 500:
                st, et, retry = 502, "server_error", True
            else:
                st, et, retry = 400, "invalid_request_error", False
            # 仅对鉴权/限流错误记账，便于冷却与转接；其他 4xx 不惩罚。
            if account is not None and upstream.status in (401, 403, 429):
                pool.report_failure(account, f"upstream {upstream.status}: {msg}")
            return "error", (st, msg, et, retry)

        if not want_stream:
            try:
                events = list(iter_ndjson_lines(upstream))
                resp = build_nonstream(model, events, created)
            except ValueError as e:  # 网关在 200 流里回了 error 事件
                if account is not None:
                    pool.report_failure(account, f"upstream error event: {e}")
                return "error", (502, str(e), "api_error", True)
            send_json(handler, 200, resp)
            if account is not None:
                pool.report_success(account)
            return "ok", None

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
            if account is not None:
                if state.get("sawError"):
                    pool.report_failure(account, f"upstream error event: {state.get('errorMessage', '')}")
                else:
                    pool.report_success(account)
            if not state.get("sawError"):
                # finish_reason 只在此刻发一次（中间步骤的已暂存丢弃）。
                sse(handler, {"choices": [{"index": 0, "delta": {}, "finish_reason": state.get("finishReason", "stop")}]})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端主动断开（ZCode 停止/超时）不是账号的问题：不记账，也不再写响应。
            log("chat: client disconnected mid-stream")
            return "stream_err", None
        except Exception as e:
            try:
                sse(handler, {"error": {"message": f"stream error: {e}", "type": "api_error"}})
            except Exception:
                pass
            if account is not None:
                pool.report_failure(account, f"stream: {e}")
        try:
            sse(handler, "[DONE]")
            handler.wfile.flush()
        except Exception:
            pass
        return ("stream_err" if state.get("sawError") else "ok"), None
    finally:
        try:
            upstream.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def handle_chat(handler, body: dict) -> None:
    created = int(time.time())
    candidates = _chat_candidates(handler)
    if not candidates:
        send_json(handler, 401, {"error": {"message": "尚未登录 CommandCode：Go 套餐没有静态 API Key，请先 `POST /login` 在浏览器完成账号 OAuth 授权（授权一次即可，key 会自动缓存）。", "type": "invalid_request_error"}})
        return

    log("chat: stream=%s model=%s candidates=%d", body.get("stream") is True, body.get("model"), len(candidates))
    last_failure = None
    for i, (api_key, account) in enumerate(candidates):
        outcome, failure = _attempt_chat(handler, body, api_key, account, created)
        if outcome != "error":
            return  # 响应已发给客户端（成功，或流开始后出错无法重试）
        status, msg, etype, retryable = failure
        last_failure = failure
        if not retryable or i == len(candidates) - 1:
            break
        # 同请求内 failover：该账号限流/鉴权失败/网关错误，换下一个候选账号重试。
        log("chat: account %s failed (HTTP %s)，转接下一个账号", account.id if account else "-", status)
    status, msg, etype, _ = last_failure
    send_json(handler, status, {"error": {"message": msg, "type": etype}})


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
# Web 控制面板
# ---------------------------------------------------------------------------
_WEB_UI_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cmdgo-provider</title>
<script>const API='http://127.0.0.1:__PORT__';</script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922}
body{background:var(--bg);color:var(--text);font:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;min-height:100vh}
a{color:var(--blue);text-decoration:none}
.container{max-width:800px;margin:0 auto;padding:24px 16px}
header{display:flex;align-items:center;gap:12px;margin-bottom:24px}
header h1{font-size:20px;font-weight:600}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:500}
.badge.ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge.err{background:rgba(248,81,73,.15);color:var(--red)}
.badge.warn{background:rgba(210,153,34,.15);color:var(--yellow)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.off{background:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}
.card h2{font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.card .val{font-size:28px;font-weight:700}
.card .sub{font-size:13px;color:var(--dim);margin-top:4px}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.btn{border:1px solid var(--border);background:var(--card);color:var(--text);padding:8px 20px;border-radius:8px;font-size:14px;cursor:pointer;transition:.15s}
.btn:hover{border-color:var(--blue);color:var(--blue)}
.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn.primary:hover{opacity:.85}
.btn.danger{border-color:var(--red);color:var(--red)}
.btn.danger:hover{background:var(--red);color:#fff}
.btn:disabled{opacity:.4;cursor:not-allowed}
.models{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;max-height:320px;overflow-y:auto}
.models h2{font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.model-list{display:flex;flex-wrap:wrap;gap:6px}
.tag{background:rgba(88,166,255,.1);color:var(--blue);padding:3px 10px;border-radius:6px;font-size:12px;font-family:monospace}
.log-section{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-top:20px}
.log-section h2{font-size:14px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
#log{font-family:"Cascadia Code","Fira Code",Consolas,monospace;font-size:12px;line-height:1.6;max-height:240px;overflow-y:auto;color:var(--dim);white-space:pre-wrap;word-break:break-all}
footer{text-align:center;color:var(--dim);font-size:12px;margin-top:32px;padding:16px}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>cmdgo-provider</h1>
    <span id="badge" class="badge err"><span class="dot off"></span>已停止</span>
  </header>

  <div class="grid">
    <div class="card">
      <h2>代理状态</h2>
      <div class="val" id="s-port">—</div>
      <div class="sub" id="s-base">COMMANDCODE_BASE_URL</div>
    </div>
    <div class="card">
      <h2>可用模型</h2>
      <div class="val" id="s-models">—</div>
      <div class="sub">Go 套餐模型</div>
    </div>
    <div class="card">
      <h2>登录状态</h2>
      <div class="val" id="s-auth">未登录</div>
      <div class="sub" id="s-user">&nbsp;</div>
    </div>
  </div>

  <div class="actions">
    <button class="btn primary" id="btn-login" onclick="doLogin()">OAuth 登录</button>
    <button class="btn danger" id="btn-logout" onclick="doLogout()" style="display:none">退出登录</button>
  </div>

  <div class="models">
    <h2>模型列表</h2>
    <div class="model-list" id="model-list">加载中…</div>
  </div>

  <div class="log-section">
    <h2>实时日志</h2>
    <div id="log"></div>
  </div>

  <footer>cmdgo-provider · 非官方 Command Code Go 适配器</footer>
</div>

<script>
const $ = s => document.querySelector(s);
let logLines = [];
const MAX_LOG = 200;

function addLog(msg) {
  const t = new Date().toLocaleTimeString();
  logLines.push('[' + t + '] ' + msg);
  if (logLines.length > MAX_LOG) logLines.shift();
  $('#log').textContent = logLines.join('\n');
  $('#log').scrollTop = $('#log').scrollHeight;
}

async function poll() {
  try {
    const [h, s, m] = await Promise.all([
      fetch(API+'/healthz').then(r => r.json()),
      fetch(API+'/login/status').then(r => r.json()),
      fetch(API+'/v1/models').then(r => r.json()),
    ]);

    // 状态
    if (h.ok) {
      $('#badge').className = 'badge ok';
      $('#badge').innerHTML = '<span class="dot on"></span>运行中';
      $('#s-port').textContent = '端口 ' + (location.port || '80');
      $('#s-models').textContent = h.models || 0;
      $('#s-base').textContent = h.baseUrl || '';
    }

    // 登录
    const hasKey = s.hasKey;
    if (hasKey) {
      $('#s-auth').textContent = '已登录';
      $('#s-auth').style.color = 'var(--green)';
      const who = s.userName || s.keyName || 'Go 套餐';
      $('#s-user').textContent = who;
      $('#btn-login').style.display = 'none';
      $('#btn-logout').style.display = '';
    } else {
      $('#s-auth').textContent = '未登录';
      $('#s-auth').style.color = 'var(--red)';
      $('#s-user').innerHTML = '&nbsp;';
      $('#btn-login').style.display = '';
      $('#btn-logout').style.display = 'none';
    }

    // 模型列表
    const models = m.data || [];
    const list = $('#model-list');
    if (models.length) {
      // 用 textContent 而不是 innerHTML：模型 id 来自上游接口，避免注入。
      list.textContent = '';
      for (const x of models) {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = x.id;
        list.appendChild(tag);
      }
    } else {
      list.textContent = '暂无模型';
    }
  } catch (e) {
    $('#badge').className = 'badge err';
    $('#badge').innerHTML = '<span class="dot off"></span>连接失败';
  }
}

async function doLogin() {
  $('#btn-login').disabled = true;
  $('#btn-login').textContent = '登录中…';
  addLog('正在发起 OAuth 登录…');
  try {
    const r = await fetch(API+'/login', {method: 'POST', headers: {'Content-Type': 'application/json'}});
    const j = await r.json();
    if (j.ok && j.authUrl) {
      addLog('请在浏览器中完成授权');
      window.open(j.authUrl, '_blank');
      // 轮询等待
      let ok = false;
      for (let i = 0; i < 400; i++) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await fetch(API+'/login/status').then(r => r.json());
        if (st.status === 'success' && st.hasKey) {
          addLog('登录成功！');
          ok = true;
          break;
        }
        if (st.status === 'error' || st.status === 'cancelled') {
          addLog((st.status === 'cancelled' ? '登录已取消: ' : '授权失败: ') + (st.message || '未知错误'));
          break;
        }
      }
      if (!ok && !$('#s-auth').textContent.includes('成功')) addLog('登录超时');
    } else {
      addLog('登录启动失败: ' + JSON.stringify(j));
    }
  } catch (e) {
    addLog('登录请求出错: ' + e);
  }
  $('#btn-login').disabled = false;
  $('#btn-login').textContent = 'OAuth 登录';
  poll();
}

async function doLogout() {
  try {
    await fetch(API+'/logout', {method: 'POST'});
    addLog('已退出登录');
  } catch (e) {}
  poll();
}

poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""


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

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length", 0))
        except Exception:
            length = 0
        return self.rfile.read(length) if length else b""

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
        if p == "/healthz":
            send_json(self, 200, {"ok": True, "service": "cmdgo-hermes-provider",
                                  "models": len(model_cache["models"]), "baseUrl": BASE_URL,
                                  "accounts": pool.size})
            return
        if p == "/account/list":
            send_json(self, 200, {"ok": True, **pool_status()})
            return
        if p in ("", "/"):
            body = _WEB_UI_HTML.replace("__PORT__", str(PORT)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        send_json(self, 404, {"error": {"message": "Not found. Try /v1/chat/completions, /v1/models, /login, /healthz", "type": "invalid_request_error"}})

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
                # 重置登录状态：否则 /login/status 会残留上次登录结果（含明文 apiKey）。
                login.last_result = {"status": "idle"}
                # 清空多号池（兼容旧语义：池空时保留手动 key）。
                removed = pool.clear()
                send_json(self, 200, {"ok": True, "removed": removed})
                return
            if p == "/account/list":
                send_json(self, 200, {"ok": True, **pool_status()})
                return
            if p == "/account/toggle":
                raw = self._read_body()
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
                sid = body.get("id", "")
                enabled = body.get("enabled") is True
                changed = sid and pool.toggle(sid, enabled)
                send_json(self, 200 if changed else 404, {"ok": True} if changed else {"ok": False, "error": "账号不存在或状态未变化"})
                return
            if p == "/account/remove":
                raw = self._read_body()
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
                sid = body.get("id", "")
                if not sid:
                    send_json(self, 400, {"ok": False, "error": "missing id"})
                    return
                removed = sid and pool.remove(sid)
                send_json(self, 200 if removed else 404, {"ok": True} if removed else {"ok": False, "error": "账号不存在"})
                return
        except Exception as e:
            # 兜底响应本身也可能写到已断开的客户端上，不能再让异常逃逸成 traceback。
            try:
                send_json(self, 500, {"error": {"message": str(e), "type": "api_error"}})
            except Exception:
                pass
            return
        send_json(self, 404, {"error": {"message": "Not found", "type": "invalid_request_error"}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 允许绑定到正在监听的端口（等于端口劫持），只在非 Windows 启用。
    allow_reuse_address = os.name != "nt"


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
    # poll_interval 越短，shutdown() 的等待窗口越短（默认 0.5s），
    # GUI 的「停止→立刻重启」就不容易撞上端口释放竞态。
    if block:
        log("listening on http://localhost:%s  (COMMANDCODE_BASE_URL=%s, CLI %s)", PORT, BASE_URL, CC_VERSION)
        if OVERRIDE_KEY:
            log("COMMANDCODE_API_KEY env set: incoming Authorization headers are ignored.")
        _server.serve_forever(poll_interval=0.05)
    else:
        threading.Thread(target=_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        log("listening on http://localhost:%s (background)", PORT)
    return _server


def _run_login_flow(port: int) -> int:
    """OAuth 登录流程：启动代理 → 打开浏览器 → 轮询等待授权完成。"""
    import webbrowser

    base = f"http://127.0.0.1:{port}"

    # 启动代理（后台）
    start_server(block=False)
    # 等待代理就绪
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        print("错误：代理服务器启动超时")
        return 1

    # 发起登录
    req = urllib.request.Request(base + "/login", data=b"",
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read())
    except Exception as e:
        print("无法发起登录：" + str(e))
        return 1
    if not j.get("ok"):
        print("登录启动失败：" + str(j))
        return 1

    auth_url = j["authUrl"]
    print("请在浏览器中完成 Command Code 授权：")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # 轮询状态
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/login/status", timeout=10) as r:
                st = json.loads(r.read())
        except Exception as e:
            print("查询登录状态出错：" + str(e))
            return 1
        if st.get("status") == "success" and st.get("hasKey"):
            print("登录成功！Go key 已缓存，现在可以直接用了。")
            return 0
        if st.get("status") == "error":
            print("授权失败：" + st.get("message", "未知错误"))
            return 1
        time.sleep(1.5)
    print("登录超时（10 分钟）。可重新运行重试。")
    return 1


def main():
    global PORT, BASE_URL, OVERRIDE_KEY, CC_VERSION
    ap = argparse.ArgumentParser(description="CommandCode Go -> OpenAI 兼容代理 (纯 Python)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--api-key", default=OVERRIDE_KEY)
    ap.add_argument("--cc-version", default=CC_VERSION)
    ap.add_argument("--login", action="store_true", help="启动代理并完成 OAuth 登录后退出")
    ap.add_argument("--keep-alive", action="store_true", help="与 --login 搭配：登录成功后保持代理运行")
    ap.add_argument("--no-gui", action="store_true", help="(兼容参数，忽略) 控制台模式；桌面入口是 cmdgo_gui.py")
    args = ap.parse_args()
    PORT = args.port
    BASE_URL = args.base_url.rstrip("/")
    if args.api_key:
        OVERRIDE_KEY = args.api_key
    CC_VERSION = args.cc_version

    if args.login:
        rc = _run_login_flow(args.port)
        if rc != 0:
            sys.exit(rc)
        if not args.keep_alive:
            return
        # --keep-alive: 登录成功后继续运行代理
        log("登录完成，代理继续运行在 http://127.0.0.1:%s", PORT)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            log("收到中断信号，退出")
            return

    # GUI 收敛后 cmdgo_provider.py 只做控制台模式（浏览器面板仍在 http://127.0.0.1:PORT/）。
    # --no-gui 保留为兼容参数：windows/start_proxy.vbs 等脚本仍在传。
    start_server(block=True)


if __name__ == "__main__":
    main()
