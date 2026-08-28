#!/usr/bin/env python3
"""Failover / error-propagation tests for cmdgo-provider (mock gateway, no Node)."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location("cmdgo_failover_mod", os.path.join(ROOT, "cmdgo_provider.py"))
cmdgo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmdgo)

# 隔离：不读真实 token.json / accounts.json
cmdgo.cached_api_key = ""
cmdgo.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-fail-token-"), "token.json")
cmdgo.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-fail-pool-"), "accounts.json")
cmdgo.pool._loaded = True
cmdgo.pool._accounts = []

PROXY_PORT = 8796
GATEWAY_PORT = 8816

# 网关收到的 Authorization 头顺序（验证 failover 的尝试次序）
gw_keys = []


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


gw = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), MockGateway)
threading.Thread(target=gw.serve_forever, daemon=True).start()
time.sleep(0.3)
cmdgo.BASE_URL = f"http://127.0.0.1:{GATEWAY_PORT}"
cmdgo.PORT = PROXY_PORT
cmdgo.OVERRIDE_KEY = ""
cmdgo.start_server(block=False)
time.sleep(0.4)

BASE = f"http://127.0.0.1:{PROXY_PORT}"
OK = True


def check(name, cond):
    global OK
    print(("PASS" if cond else "FAIL"), name, flush=True)
    if not cond:
        OK = False


def chat(model="test/m", stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer cmdgo"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ---- 场景 1：池内 A(限流) + B(正常)：同一请求内 failover 成功 ----
cmdgo.pool.add({"apiKey": "KEY-BAD", "userName": "bad"})
cmdgo.pool.add({"apiKey": "KEY-OK", "userName": "good"})
st, resp = chat()
check("failover returns 200", st == 200)
check("failover content from second account",
      resp["choices"][0]["message"]["content"] == "failover-ok")
check("failover tried keys in order", gw_keys == ["Bearer KEY-BAD", "Bearer KEY-OK"])
bad = cmdgo.pool.find_by_key("KEY-BAD")
good = cmdgo.pool.find_by_key("KEY-OK")
check("bad account penalized", bad.failCount == 1 and bad.cooldownUntil is not None)
check("good account healthy", good.failCount == 0 and good.cooldownUntil is None)

# ---- 场景 2：模型不存在（网关回 401 not recognized）-> 代理改写 400 且不换号重试 ----
gw_keys.clear()
bad.enabled = False
good.enabled = False
cmdgo.cached_api_key = "KEY-UNKNOWN-MODEL"
st, resp = chat(model="no-such/model")
check("unknown model mapped to 400", st == 400)
check("unknown model message kept", "not recognized" in resp["error"]["message"])
check("unknown model no failover attempts", len(gw_keys) == 1)

# ---- 场景 3：200 流中夹带 error 事件（非流式）-> 502 而不是空成功 ----
cmdgo.cached_api_key = "KEY-STREAM-ERR"
st, resp = chat()
check("mid-stream error event -> 502", st == 502)
check("mid-stream error message", "boom mid-stream" in resp["error"]["message"])

# ---- 场景 4：同上（流式）-> SSE 里转发 error，且不发 finish_reason ----
body = {"model": "test/m", "messages": [{"role": "user", "content": "hi"}], "stream": True}
req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
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
check("sse error event forwarded", saw_error)
check("sse omits finish_reason after error", not saw_finish)

# ---- 场景 5：池非空但全部停用 -> 回落到缓存 key，而不是 401 ----
cmdgo.cached_api_key = "KEY-OK"
st, resp = chat()
check("all-disabled pool falls back to cached key", st == 200 and
      resp["choices"][0]["message"]["content"] == "failover-ok")

# ---- 场景 6：/login/status 不回传明文 apiKey ----
cmdgo.login.last_result = {"status": "success", "userName": "u", "apiKey": "SECRET-KEY", "at": 1}
with urllib.request.urlopen(BASE + "/login/status", timeout=10) as r:
    st_payload = json.loads(r.read())
check("login/status hides apiKey", "apiKey" not in st_payload and st_payload.get("status") == "success")

gw.shutdown()
print("\nALL PASS" if OK else "\nSOME FAILED", flush=True)
sys.exit(0 if OK else 1)
