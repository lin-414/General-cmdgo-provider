#!/usr/bin/env python3
"""Mock-gateway end-to-end test for cmdgo-provider (pure Python, no Node)."""
import importlib.util
import json
import os
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROXY_PORT = 8799
GATEWAY_PORT = 8801


class MockGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, chunk: str):
        try:
            self.wfile.write((chunk + "\n").encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b""
        envelope = json.loads(raw.decode("utf-8", "replace"))
        assert self.headers.get("authorization", "").startswith("Bearer "), "missing auth"
        assert self.headers.get("user-agent", "").startswith("commandcode/"), "bad UA"
        assert self.headers.get("x-project-slug"), "bad slug"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for line in [
            '{"type":"text-start"}',
            '{"type":"reasoning-delta","text":"让我想想"}',
            '{"type":"text-delta","text":"Hello"}',
            '{"type":"text-delta","text":" world"}',
            '{"type":"tool-call","toolCallId":"call_abc","toolName":"get_weather","input":{"city":"Beijing"}}',
            '{"type":"finish-step","finishReason":"tool_calls","usage":{"inputTokens":12,"outputTokens":7}}',
        ]:
            self._send(line)
            time.sleep(0.01)


@pytest.fixture(scope="module")
def cmdgo():
    spec = importlib.util.spec_from_file_location("cmdgo_test_mock", os.path.join(ROOT, "cmdgo_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 隔离：不读真实 token.json / accounts.json
    mod.cached_api_key = ""
    mod.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-mock-token-"), "token.json")
    mod.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-mock-pool-"), "accounts.json")
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
    cmdgo.OVERRIDE_KEY = "test-key-123"
    cmdgo.start_server(block=False)
    time.sleep(0.4)
    yield cmdgo
    cmdgo._server.shutdown()
    cmdgo._server.server_close()
    cmdgo._server = None


def delta_of(e):
    ch = e.get("choices") or [{}]
    return (ch[0] if ch else {}).get("delta", {})


def finish_of(e):
    ch = e.get("choices") or [{}]
    return (ch[0] if ch else {}).get("finish_reason")


def test_healthz(proxy):
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/healthz", timeout=10) as r:
        h = json.loads(r.read())
    assert h.get("ok") is True


def test_models(proxy):
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/v1/models", timeout=10) as r:
        m = json.loads(r.read())
    assert m.get("object") == "list"
    assert isinstance(m.get("data"), list)


def test_nonstream(proxy):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
        data=json.dumps({"model": "test/go-model", "messages": [{"role": "user", "content": "hi"}], "stream": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    msg = resp["choices"][0]["message"]
    assert msg.get("content") == "Hello world"
    assert msg.get("reasoning_content") == "让我想想"
    assert isinstance(msg.get("tool_calls"), list) and msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert resp["choices"][0]["finish_reason"] == "tool_calls"
    assert resp["usage"]["total_tokens"] == 19


def test_stream(proxy):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
        data=json.dumps({"model": "test/go-model", "messages": [{"role": "user", "content": "hi"}],
                         "stream": True, "stream_options": {"include_usage": True}}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events = []
    with urllib.request.urlopen(req, timeout=10) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            events.append("[DONE]" if payload == "[DONE]" else json.loads(payload))

    reasoning = [e for e in events if e != "[DONE]" and "reasoning_content" in delta_of(e)]
    contents = [e for e in events if e != "[DONE]" and "content" in delta_of(e)]
    tools = [e for e in events if e != "[DONE]" and delta_of(e).get("tool_calls")]
    finishes = [e for e in events if e != "[DONE]" and finish_of(e)]
    usages = [e for e in events if e != "[DONE]" and "usage" in e]

    assert any(e != "[DONE]" and delta_of(e).get("role") for e in events)
    assert len(reasoning) == 1 and reasoning[0]["choices"][0]["delta"]["reasoning_content"] == "让我想想"
    assert "".join(c["choices"][0]["delta"]["content"] for c in contents) == "Hello world"
    assert len(tools) == 1 and tools[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather"
    # finish_reason 是流终态：整条流只出现一次
    assert len(finishes) == 1 and finishes[0]["choices"][0]["finish_reason"] == "tool_calls"
    assert len(usages) == 1 and usages[0]["usage"]["total_tokens"] == 19
    assert events and events[-1] == "[DONE]"
