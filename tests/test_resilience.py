#!/usr/bin/env python3
"""韧性改进测试：npm 版本跟随 / 空闲超时 / 空输出重试 / 402 failover / x-cmd-zdr。

复用 test_failover.py 的 mock 网关模式：代理与 mock 上游都在本地端口上真实起服。
"""
import importlib.util
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROXY_PORT = 8797
GATEWAY_PORT = 8817
NPM_PORT = 8818

OK_LINES = (b'{"type":"text-delta","text":"ok-output"}\n'
            b'{"type":"finish-step","finishReason":"stop","usage":{"inputTokens":1,"outputTokens":1}}\n')
ZERO_LINES = (b'{"type":"finish-step","finishReason":"stop","usage":{"inputTokens":1,"outputTokens":0}}\n')


class MockGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _ndjson(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length) if length else b""
        auth = self.headers.get("authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else ""
        gw_keys.append(key)
        gw_headers[key] = {k.lower(): v for k, v in self.headers.items()}
        if key == "KEY-ZERO":
            self._ndjson(ZERO_LINES)
        elif key == "KEY-402":
            self._json(402, b'{"error":{"message":"payment required"}}')
        elif key == "KEY-SLOW":
            # 先发响应头，再让 body 静默 3 秒：触发代理侧的读空闲超时。
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                self.wfile.flush()
            except Exception:
                pass
            time.sleep(3)
            self._ndjson(OK_LINES)
        else:  # KEY-OK / KEY-ZDR / KEY-NOZDR
            self._ndjson(OK_LINES)


# 网关收到的 key 顺序与请求头（按 key 记录）；模块内跨用例共享
gw_keys: list = []
gw_headers: dict = {}


class MockNpm(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps(npm_state["payload"]).encode("utf-8")
        self.send_response(npm_state["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


npm_state = {"payload": {"version": "9.9.9"}, "status": 200}


@pytest.fixture(scope="module")
def cmdgo():
    spec = importlib.util.spec_from_file_location("cmdgo_resilience_mod", os.path.join(ROOT, "cmdgo_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cached_api_key = ""
    mod.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-res-token-"), "token.json")
    mod.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-res-pool-"), "accounts.json")
    mod.pool._loaded = True
    mod.pool._accounts = []
    # 空闲超时压短，避免测试拖慢（mock 上游静默 3s）
    mod.STREAM_IDLE_TIMEOUT_S = 1.0
    mod.NONSTREAM_IDLE_TIMEOUT_S = 2.0
    mod.ZDR_ENABLED = False
    mod.CC_VERSION_AUTO = False  # 测试内不触发对真实 npm registry 的后台请求
    return mod


@pytest.fixture(scope="module")
def gateway():
    gw = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), MockGateway)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    npm = ThreadingHTTPServer(("127.0.0.1", NPM_PORT), MockNpm)
    threading.Thread(target=npm.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield (gw, npm)
    gw.shutdown()
    gw.server_close()
    npm.shutdown()
    npm.server_close()


@pytest.fixture(scope="module")
def proxy(cmdgo, gateway):
    cmdgo.BASE_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
    cmdgo.PORT = PROXY_PORT
    cmdgo.OVERRIDE_KEY = ""
    cmdgo.start_server(block=False)
    time.sleep(0.4)
    yield cmdgo
    cmdgo._server.shutdown()
    cmdgo._server.server_close()
    cmdgo._server = None


def _chat(stream=False, model="test/m"):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer cmdgo"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def test_s1_cc_version_refresh_from_npm(cmdgo, gateway):
    """npm registry 可用时刷新 CC_VERSION；坏 payload 时沿用当前值。"""
    cmdgo.NPM_CC_VERSION_URL = f"http://127.0.0.1:{NPM_PORT}/command-code/latest"
    npm_state.update({"payload": {"version": "9.9.9"}, "status": 200})
    assert cmdgo.refresh_cc_version() is True
    assert cmdgo.CC_VERSION == "9.9.9"
    npm_state.update({"payload": {"error": "broken"}, "status": 200})
    assert cmdgo.refresh_cc_version() is False
    assert cmdgo.CC_VERSION == "9.9.9"
    npm_state.update({"payload": {"version": "9.9.9"}, "status": 500})
    assert cmdgo.refresh_cc_version() is False
    assert cmdgo.CC_VERSION == "9.9.9"


def test_s2_zdr_header_toggle(cmdgo, proxy, gateway):
    """ZDR 开关：开启时 generate 带 x-cmd-zdr: 1，关闭时不带。"""
    cmdgo.pool.clear()
    cmdgo.cached_api_key = "KEY-ZDR"
    cmdgo.ZDR_ENABLED = True
    st, _, _ = _chat()
    assert st == 200
    assert gw_headers["KEY-ZDR"].get("x-cmd-zdr") == "1"
    cmdgo.cached_api_key = "KEY-NOZDR"
    cmdgo.ZDR_ENABLED = False
    st, _, _ = _chat()
    assert st == 200
    assert "x-cmd-zdr" not in gw_headers["KEY-NOZDR"]


def test_s3_zero_output_single_candidate_429(cmdgo, proxy, gateway):
    """空回复（只有 finish-step）且无候选可换：回 429 而不是 200 空内容。"""
    cmdgo.pool.clear()
    cmdgo.cached_api_key = "KEY-ZERO"
    st, ctype, raw = _chat()
    assert st == 429
    assert "application/json" in ctype
    assert "zero visible output" in json.loads(raw)["error"]["message"]


def test_s4_zero_output_fails_over_nonstream(cmdgo, proxy, gateway):
    """池内 A(空回复) + B(正常)：非流式 failover 到 B，且 A 不计入冷却。"""
    cmdgo.pool.clear()
    cmdgo.pool.add({"apiKey": "KEY-ZERO", "userName": "zero"})
    cmdgo.pool.add({"apiKey": "KEY-OK", "userName": "good"})
    cmdgo.cached_api_key = ""
    gw_keys.clear()
    st, _, raw = _chat()
    assert st == 200
    assert json.loads(raw)["choices"][0]["message"]["content"] == "ok-output"
    assert gw_keys == ["KEY-ZERO", "KEY-OK"]
    zero = cmdgo.pool.find_by_key("KEY-ZERO")
    assert zero.failCount == 0 and zero.cooldownUntil is None


def test_s5_zero_output_fails_over_stream(cmdgo, proxy, gateway):
    """流式空回复 failover：客户端只看到 B 的正常 SSE（无 error 事件）。"""
    cmdgo.pool.clear()
    cmdgo.pool.add({"apiKey": "KEY-ZERO", "userName": "zero"})
    cmdgo.pool.add({"apiKey": "KEY-OK", "userName": "good"})
    cmdgo.cached_api_key = ""
    gw_keys.clear()
    st, ctype, raw = _chat(stream=True)
    assert st == 200
    assert "text/event-stream" in ctype
    saw_output = saw_error = saw_finish = False
    for line in raw.decode("utf-8", "replace").split("\n\n"):
        s = line.strip()
        if not s.startswith("data: ") or s == "data: [DONE]":
            continue
        d = json.loads(s[6:])
        if "error" in d:
            saw_error = True
        for ch in d.get("choices", []):
            if ch.get("delta", {}).get("content"):
                saw_output = True
            if ch.get("finish_reason"):
                saw_finish = True
    assert saw_output and saw_finish and not saw_error
    assert gw_keys == ["KEY-ZERO", "KEY-OK"]


def test_s6_402_fails_over_and_cooldowns(cmdgo, proxy, gateway):
    """402（余额耗尽）按限流处理：换号重试 + 记入账号冷却。"""
    cmdgo.pool.clear()
    cmdgo.pool.add({"apiKey": "KEY-402", "userName": "broke"})
    cmdgo.pool.add({"apiKey": "KEY-OK", "userName": "good"})
    cmdgo.cached_api_key = ""
    gw_keys.clear()
    st, _, raw = _chat()
    assert st == 200
    assert json.loads(raw)["choices"][0]["message"]["content"] == "ok-output"
    assert gw_keys == ["KEY-402", "KEY-OK"]
    broke = cmdgo.pool.find_by_key("KEY-402")
    assert broke.cooldownUntil is not None


def test_s7_idle_timeout_nonstream(cmdgo, proxy, gateway):
    """非流式读空闲超时：上游静默 -> 429（可重试），不会挂满 600s。"""
    cmdgo.pool.clear()
    cmdgo.cached_api_key = "KEY-SLOW"
    t0 = time.time()
    st, ctype, raw = _chat()
    took = time.time() - t0
    assert st == 429
    assert "application/json" in ctype
    assert "idle timeout" in json.loads(raw)["error"]["message"]
    assert took < 10  # 空闲 2s 触发，远小于旧默认的 600s


def test_s8_idle_timeout_stream_before_visible(cmdgo, proxy, gateway):
    """流式在首个可见输出前超时：未发任何 SSE 字节 -> JSON 429（可换号重试）。"""
    cmdgo.pool.clear()
    cmdgo.cached_api_key = "KEY-SLOW"
    st, ctype, raw = _chat(stream=True)
    assert st == 429
    assert "application/json" in ctype  # 不是 text/event-stream：还没开始流式
    assert "idle timeout" in json.loads(raw)["error"]["message"]
