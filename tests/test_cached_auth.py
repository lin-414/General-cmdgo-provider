#!/usr/bin/env python3
"""Cached-auth flow: placeholder Authorization must never override the cached OAuth key."""
import importlib.util
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROXY_PORT = 8795
GATEWAY_PORT = 8805


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        if length:
            self.rfile.read(length)
        # verify it received the cached Bearer key from the proxy
        ok = self.headers.get("authorization") == "Bearer CACHED-GO-KEY-123"
        body = ('{"type":"text-delta","text":"authed-ok"}' if ok else '{"type":"text-delta","text":"NO-KEY-BUG"}') + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        self.wfile.write(body.encode())
        self.wfile.flush()


@pytest.fixture(scope="module")
def cmdgo():
    spec = importlib.util.spec_from_file_location("cmdgo_test_cached", os.path.join(ROOT, "cmdgo_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-test-"), "token.json")
    mod.cached_api_key = ""
    # isolate the account pool: point it at a throwaway file and empty it
    mod.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-pool-"), "accounts.json")
    mod.pool._loaded = True
    mod.pool._accounts = []
    return mod


@pytest.fixture(scope="module")
def gateway():
    gw = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), Gateway)
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
    cmdgo.login.stop("test teardown")
    cmdgo._server.shutdown()
    cmdgo._server.server_close()
    cmdgo._server = None


def _post(path, data=b""):
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}{path}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}{path}", timeout=10) as r:
        return json.loads(r.read())


def test_cached_auth_flow(cmdgo, proxy):
    # 1) start login (starts callback server)
    lg = _post("/login")
    assert lg.get("ok") is True
    assert "commandcode.ai/studio/auth/cli" in lg.get("authUrl", "")
    # extract state
    q = urllib.parse.parse_qs(urllib.parse.urlparse(lg["authUrl"]).query)
    state = q["state"][0]
    callback = lg["callbackUrl"]

    # 2) simulate the browser OAuth callback (CommandCode POSTs apiKey+state to callback)
    cbreq = urllib.request.Request(callback, data=json.dumps({"apiKey": "CACHED-GO-KEY-123", "state": state}).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(cbreq, timeout=10) as r:
        assert r.status == 200

    # 3) status should now have hasKey True
    deadline = time.time() + 5
    st = {}
    while time.time() < deadline:
        st = _get("/login/status")
        if st.get("hasKey"):
            break
        time.sleep(0.2)
    assert st.get("hasKey") is True, "key should be cached!"

    # 4) chat with Studio's placeholder Authorization -> must still use cached key
    chatreq = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
        data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer cmdgo"}, method="POST")
    with urllib.request.urlopen(chatreq, timeout=10) as r:
        resp = json.loads(r.read())
    content = resp["choices"][0]["message"]["content"]
    assert content == "authed-ok", "cached key was NOT used!"

    # 4b) 登录成功后 /login/status 不得回传明文 apiKey
    st = _get("/login/status")
    assert "apiKey" not in st

    # 5) logout clears cache (and must not leak the old key in status)
    _post("/logout")
    st2 = _get("/login/status")
    assert st2.get("hasKey") is False
    assert "apiKey" not in st2
