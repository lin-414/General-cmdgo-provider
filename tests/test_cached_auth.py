import importlib.util, json, os, tempfile, threading, time, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE=os.path.dirname(os.path.abspath(__file__))
ROOT=os.path.dirname(HERE)
spec=importlib.util.spec_from_file_location("m", os.path.join(ROOT,"cmdgo_provider.py"))
cmdgo=importlib.util.module_from_spec(spec); spec.loader.exec_module(cmdgo)
cmdgo.TOKEN_FILE = os.path.join(tempfile.mkdtemp(prefix="cmdgo-test-"), "token.json")
cmdgo.cached_api_key = ""
# isolate the account pool: point it at a throwaway file and empty it
_pool_tmp = os.path.join(tempfile.mkdtemp(prefix="cmdgo-pool-"), "accounts.json")
cmdgo.pool._file = _pool_tmp
cmdgo.pool._loaded = True
cmdgo.pool._accounts = []
GP=8805
class G(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_POST(self):
        l=int(self.headers.get("content-length",0)); self.rfile.read(l) if l else b""
        # verify it received the cached Bearer key from the proxy
        ok = self.headers.get("authorization")=="Bearer CACHED-GO-KEY-123"
        body=('{"type":"text-delta","text":"authed-ok"}' if ok else '{"type":"text-delta","text":"NO-KEY-BUG"}')+'\n'
        self.send_response(200); self.send_header("Content-Type","application/x-ndjson"); self.end_headers()
        self.wfile.write(body.encode()); self.wfile.flush()
gw=ThreadingHTTPServer(("127.0.0.1",GP),G); threading.Thread(target=gw.serve_forever,daemon=True).start(); time.sleep(0.3)
cmdgo.BASE_URL=f"http://127.0.0.1:{GP}"; cmdgo.PORT=8795; cmdgo.OVERRIDE_KEY=""; cmdgo.start_server(block=False); time.sleep(0.4)

def post(path, data=b""):
    req=urllib.request.Request(f"http://127.0.0.1:8795{path}", data=data, headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r: return json.loads(r.read())
def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8795{path}", timeout=10) as r: return json.loads(r.read())

# 1) start login (starts callback server)
lg=post("/login")
print("login ->", lg.get("ok"), "| authUrl present:", "commandcode.ai/studio/auth/cli" in lg.get("authUrl",""))
# extract state
q=urllib.parse.parse_qs(urllib.parse.urlparse(lg["authUrl"]).query)
state=q["state"][0]; cb=lg["callbackUrl"]
# 2) simulate the browser OAuth callback (CommandCode POSTs apiKey+state to callback)
cbreq=urllib.request.Request(cb, data=json.dumps({"apiKey":"CACHED-GO-KEY-123","state":state}).encode(), headers={"Content-Type":"application/json"}, method="POST")
with urllib.request.urlopen(cbreq, timeout=10) as r: print("callback ->", r.status, r.read().decode())
# 3) status should now have hasKey True
st=get("/login/status"); print("login/status ->", st)
assert st.get("hasKey") is True, "key should be cached!"

# 4) chat with Studio's placeholder Authorization -> must still use cached key
chatreq=urllib.request.Request("http://127.0.0.1:8795/v1/chat/completions",
    data=json.dumps({"model":"m","messages":[{"role":"user","content":"hi"}],"stream":False}).encode(),
    headers={"Content-Type":"application/json", "Authorization":"Bearer cmdgo"}, method="POST")
with urllib.request.urlopen(chatreq, timeout=10) as r:
    resp=json.loads(r.read())
content=resp["choices"][0]["message"]["content"]
print("chat content (placeholder auth header) ->", repr(content))
assert content=="authed-ok", "cached key was NOT used!"

# 5) logout clears cache
post("/logout"); st2=get("/login/status"); print("after logout ->", st2); assert st2.get("hasKey") is False
gw.shutdown()
print("\nCACHED-AUTH FLOW OK")
