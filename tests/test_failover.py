#!/usr/bin/env python3
"""Failover / error-propagation tests for cmdgo-provider (mock gateway, no Node)."""
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

PROXY_PORT = 8796
GATEWAY_PORT = 8816


class MockGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status: int, body: bytes, content_type=b"application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type.decode())
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length) if length else b""
        auth = self.headers.get("authorization", "")
        gw_keys.append(auth)
        key = auth[7:] if auth.startswith("Bearer ") else ""
        if key == "KEY-BAD":
            self._json(429, b'{"error":{"message":"rate limited"}}')
        elif key == "KEY-UNKNOWN-MODEL":
            self._json(401, b'{"error":{"message":"Model/provider not recognized: anthropic:no-such/model"}}')
        elif key == "KEY-STREAM-ERR":
            self._json(200, b'{"type":"text-delta","text":"partial"}\n'
                            b'{"type":"error","message":"boom mid-stream"}\n',
                       b"application/x-ndjson")
        else:  # KEY-OK
            self._json(200, b'{"type":"text-delta","text":"failover-ok"}\n'
                            b'{"type":"finish-step","finishReason":"stop","usage":{"inputTokens":1,"outputTokens":1}}\n',
                       b"application/x-ndjson")


# 网关收到的 Authorization 头顺序（验证 failover 的尝试次序）；模块内跨用例共享
gw_keys = []


@pytest.fixture(scope="module")
def cmdgo():
    spec = importlib.util.spec_from_file_location("cmdgo_failover_mod", os.path.join(ROOT, "cmdgo_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cached_api_key = ""
    mod.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-fail-token-"), "token.json")
    mod.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-fail-pool-"), "accounts.json")
    mod.pool._loaded = True
    mod.pool._accounts = []
    return mod


@pytest.fixture(scope="module")
def gateway():
    gw = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), MockGateway)
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield gw
    gw.shutdown()
    gw.server_close()


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


@pytest.fixture(scope="module")
def state():
    """用例按定义顺序执行、依次搭建状态：bad/good 账号引用在场景间复用。"""
    return {}


def _chat(model="test/m", stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer cmdgo"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_s1_failover_within_request(cmdgo, proxy, state):
    """池内 A(限流) + B(正常)：同一请求内 failover 成功。"""
    cmdgo.pool.add({"apiKey": "KEY-BAD", "userName": "bad"})
    cmdgo.pool.add({"apiKey": "KEY-OK", "userName": "good"})
    gw_keys.clear()
    st, resp = _chat()
    assert st == 200
    assert resp["choices"][0]["message"]["content"] == "failover-ok"
    assert gw_keys == ["Bearer KEY-BAD", "Bearer KEY-OK"]
    state["bad"] = cmdgo.pool.find_by_key("KEY-BAD")
    state["good"] = cmdgo.pool.find_by_key("KEY-OK")
    assert state["bad"].failCount == 1 and state["bad"].cooldownUntil is not None
    assert state["good"].failCount == 0 and state["good"].cooldownUntil is None


def test_s2_unknown_model_maps_to_400_no_failover(cmdgo, proxy, state):
    """模型不存在（网关回 401 not recognized）-> 代理改写 400 且不换号重试。"""
    gw_keys.clear()
    state["bad"].enabled = False
    state["good"].enabled = False
    cmdgo.cached_api_key = "KEY-UNKNOWN-MODEL"
    st, resp = _chat(model="no-such/model")
    assert st == 400
    assert "not recognized" in resp["error"]["message"]
    assert len(gw_keys) == 1


def test_s3_midstream_error_event_nonstream(cmdgo, proxy):
    """200 流中夹带 error 事件（非流式）-> 502 而不是空成功。"""
    cmdgo.cached_api_key = "KEY-STREAM-ERR"
    st, resp = _chat()
    assert st == 502
    assert "boom mid-stream" in resp["error"]["message"]


def test_s4_midstream_error_event_stream(proxy):
    """同上（流式）-> SSE 里转发 error，且不发 finish_reason。"""
    body = {"model": "test/m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer cmdgo"},
                                 method="POST")
    saw_error = False
    saw_finish = False
    with urllib.request.urlopen(req, timeout=30) as r:
        for line in r:
            s = line.decode("utf-8", "replace").strip()
            if not s.startswith("data: ") or s == "data: [DONE]":
                continue
            d = json.loads(s[6:])
            if "error" in d:
                saw_error = True
            ch = (d.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                saw_finish = True
    assert saw_error
    assert not saw_finish


def test_s5_all_disabled_pool_falls_back_to_cached_key(cmdgo, proxy):
    """池非空但全部停用 -> 回落到缓存 key，而不是 401。"""
    cmdgo.cached_api_key = "KEY-OK"
    st, resp = _chat()
    assert st == 200
    assert resp["choices"][0]["message"]["content"] == "failover-ok"


def test_s6_login_status_hides_api_key(cmdgo, proxy):
    """/login/status 不回传明文 apiKey。"""
    cmdgo.login.last_result = {"status": "success", "userName": "u", "apiKey": "SECRET-KEY", "at": 1}
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/login/status", timeout=10) as r:
        st_payload = json.loads(r.read())
    assert "apiKey" not in st_payload
    assert st_payload.get("status") == "success"
