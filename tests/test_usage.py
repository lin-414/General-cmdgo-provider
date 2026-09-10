#!/usr/bin/env python3
"""Usage/quota aggregation test for /usage/overview (mock upstream, no network)."""
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

PROXY_PORT = 8797
GATEWAY_PORT = 8817


class MockGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/alpha/usage/summary"):
            self._json(200, {"totalCount": 10, "completedCount": 9, "failedCount": 1, "successRate": 90,
                             "totalTokensIn": 1000, "totalTokensOut": 200, "totalTokens": 1200,
                             "totalCredits": 1.25, "periodBasis": "billing-period"})
        elif self.path.startswith("/alpha/billing/credits"):
            self._json(200, {
                "credits": {"monthlyCredits": 6.0, "purchasedCredits": 0, "freeCredits": 0},
                "windowLimits": {"limited": True,
                                 "fiveHour": {"used": 1.0, "cap": 3, "exceeded": False, "resetAt": 123},
                                 "weekly": {"used": 2.0, "cap": 6, "exceeded": False, "resetAt": 456}},
            })
        else:
            self._json(404, {"error": {"message": "not a registered API route"}})


@pytest.fixture(scope="module")
def cmdgo():
    spec = importlib.util.spec_from_file_location("cmdgo_test_usage", os.path.join(ROOT, "cmdgo_provider.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.cached_api_key = "USAGE-KEY"
    mod.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-usage-token-"), "token.json")
    mod.pool._file = os.path.join(tempfile.mkdtemp(prefix="cmdgo-usage-pool-"), "accounts.json")
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


def _get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}{path}", timeout=10) as r:
        return json.loads(r.read())


def test_usage_overview_aggregates(cmdgo, proxy):
    cmdgo.pool.add({"apiKey": "USAGE-KEY", "userName": "usage-user"})
    cmdgo._usage_cache["data"] = None
    d = _get("/usage/overview")
    assert d["ok"] is True
    assert d["usage"]["totalCount"] == 10
    assert d["usage"]["totalTokens"] == 1200
    assert d["credits"]["windowLimits"]["fiveHour"]["cap"] == 3
    assert d["credits"]["windowLimits"]["weekly"]["used"] == 2.0
    # 本地账号统计一并带回，且快照不含明文 key
    assert d["local"]["size"] == 1
    assert "apiKey" not in d["local"]["accounts"][0]


def test_usage_overview_cached_then_forced(cmdgo, proxy):
    cmdgo._usage_cache["data"] = None
    first = _get("/usage/overview")
    assert first["ok"] is True
    # 缓存命中：不强制刷新时不应变化
    cached = _get("/usage/overview")
    assert cached["at"] == first["at"]
    forced = _get("/usage/overview?refresh=1")
    assert forced["ok"] is True


def test_usage_overview_without_key(cmdgo, proxy):
    saved_cached = cmdgo.cached_api_key
    accounts = list(cmdgo.pool.list())
    cmdgo.cached_api_key = ""
    for a in accounts:
        a.enabled = False
    cmdgo._usage_cache["data"] = None
    try:
        d = _get("/usage/overview")
        assert d["ok"] is False
        assert "尚未登录" in d["error"]
    finally:
        cmdgo.cached_api_key = saved_cached
        for a in accounts:
            a.enabled = True
        cmdgo._usage_cache["data"] = None
