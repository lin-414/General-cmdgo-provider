#!/usr/bin/env python3
"""Mock-gateway end-to-end test for cmdgo-provider (pure Python, no Node)."""
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("cmdgo_test_mod", os.path.join(ROOT, "cmdgo_provider.py"))
cmdgo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmdgo)

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
        model = envelope["params"]["model"]
        assert self.headers.get("authorization", "").startswith("Bearer "), "missing auth"
        assert self.headers.get("user-agent") == f"commandcode/{cmdgo.CC_VERSION}", "bad UA"
        assert self.headers.get("x-project-slug") == cmdgo.PROJECT_SLUG, "bad slug"
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


gw = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), MockGateway)
threading.Thread(target=gw.serve_forever, daemon=True).start()
time.sleep(0.3)

cmdgo.BASE_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
cmdgo.PORT = PROXY_PORT
cmdgo.OVERRIDE_KEY = "test-key-123"
cmdgo.start_server(block=False)
time.sleep(0.4)

BASE = f"http://127.0.0.1:{PROXY_PORT}"
OK = True


def check(name, cond):
    global OK
    print(("PASS" if cond else "FAIL"), name, flush=True)
    if not cond:
        OK = False


try:
    with urllib.request.urlopen(f"{BASE}/healthz", timeout=10) as r:
        h = json.loads(r.read())
        check("healthz ok", h.get("ok") is True)

    with urllib.request.urlopen(f"{BASE}/v1/models", timeout=10) as r:
        m = json.loads(r.read())
        check("models object list", m.get("object") == "list" and isinstance(m.get("data"), list))

    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=json.dumps({"model": "test/go-model", "messages": [{"role": "user", "content": "hi"}], "stream": False}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
        msg = resp["choices"][0]["message"]
        check("nonstream content", msg.get("content") == "Hello world")
        check("nonstream reasoning", msg.get("reasoning_content") == "让我想想")
        check("nonstream tool_calls", isinstance(msg.get("tool_calls"), list) and msg["tool_calls"][0]["function"]["name"] == "get_weather")
        check("nonstream finish", resp["choices"][0]["finish_reason"] == "tool_calls")
        check("nonstream usage", resp["usage"]["total_tokens"] == 19)

    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
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
            if payload == "[DONE]":
                events.append("[DONE]")
            else:
                events.append(json.loads(payload))

    def delta_of(e):
        ch = e.get("choices") or [{}]
        return (ch[0] if ch else {}).get("delta", {})

    def finish_of(e):
        ch = e.get("choices") or [{}]
        return (ch[0] if ch else {}).get("finish_reason")

    roles = [e for e in events if e != "[DONE]" and delta_of(e).get("role")]
    reasoning = [e for e in events if e != "[DONE]" and "reasoning_content" in delta_of(e)]
    contents = [e for e in events if e != "[DONE]" and "content" in delta_of(e)]
    tools = [e for e in events if e != "[DONE]" and delta_of(e).get("tool_calls")]
    finishes = [e for e in events if e != "[DONE]" and finish_of(e)]
    usages = [e for e in events if e != "[DONE]" and "usage" in e]
    check("stream role chunk", len(roles) >= 1)
    check("stream reasoning chunk", len(reasoning) == 1 and reasoning[0]["choices"][0]["delta"]["reasoning_content"] == "让我想想")
    check("stream content chunks", "".join(c["choices"][0]["delta"]["content"] for c in contents) == "Hello world")
    check("stream tool_call chunk", len(tools) == 1 and tools[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "get_weather")
    check("stream usage chunk", len(usages) == 1 and usages[0]["usage"]["total_tokens"] == 19)
    check("stream finish chunk", len(finishes) == 1 and finishes[0]["choices"][0]["finish_reason"] == "tool_calls")
    check("stream DONE", bool(events) and events[-1] == "[DONE]")
except Exception as e:
    print("EXCEPTION:", repr(e), flush=True)
    OK = False
finally:
    gw.shutdown()
    print("\nALL PASS" if OK else "\nSOME FAILED", flush=True)
    sys.exit(0 if OK else 1)
