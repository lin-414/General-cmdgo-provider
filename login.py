#!/usr/bin/env python3
"""
一键 OAuth 登录助手（Go 套餐没有静态 Key，必须走账号授权）。

用法：
  python login.py                 # 默认连 http://localhost:8787
  python login.py --port 8787

流程：
  1. POST /login  -> 拿到 authUrl
  2. 自动用默认浏览器打开 authUrl，你在网页里用 Command Code 账号授权（需有 Go 套餐）
  3. 轮询 GET /login/status 直到拿到 key（自动缓存进 token.json）
登录成功后即可在 Hermes Studio 里用本供应商对话，无需再填任何 key。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

try:
    import tkinter.messagebox as _mb
    _HAS_TK = True
except Exception:
    _HAS_TK = False

PORT = 8787


def _info(msg: str):
    print(msg)
    if _HAS_TK:
        try:
            _mb.showinfo("General-cmdgo-provider 登录", msg)
        except Exception:
            pass


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _proxy_is_ready(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _start_proxy_if_needed(port: int, base: str) -> bool:
    if _proxy_is_ready(base):
        return True

    folder = _base_dir()
    provider_exe = os.path.join(folder, "cmdgo-provider.exe")
    gui_exe = os.path.join(folder, "cmdgo-gui.exe")
    run_py = os.path.join(folder, "run.py")
    log_dir = os.path.join(folder, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "proxy.log")

    try:
        if os.path.isfile(provider_exe):
            command = [provider_exe, "--port", str(port)]
        elif os.path.isfile(gui_exe):
            # 新发行版只带 GUI 版 exe：GUI 启动后会自动拉起本地代理。
            command = [gui_exe, "--port", str(port)]
        elif os.path.isfile(run_py) and not getattr(sys, "frozen", False):
            command = [sys.executable, run_py, "--port", str(port)]
        else:
            return False
        with open(log_path, "a", encoding="utf-8") as log_file:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                command,
                cwd=folder,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                creationflags=flags,
                close_fds=True,
            )
    except Exception as e:
        _info("无法自动启动代理：" + str(e))
        return False

    deadline = time.time() + 15
    while time.time() < deadline:
        if _proxy_is_ready(base):
            return True
        time.sleep(0.5)
    return False


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--base", default=None)
    args = ap.parse_args()
    base = (args.base or f"http://127.0.0.1:{args.port}").rstrip("/")

    if not _start_proxy_if_needed(args.port, base):
        _info(
            "本地代理没有运行，且无法自动启动。\n"
            "请确认 cmdgo-provider.exe 与 cmdgo-login.exe 在同一个文件夹，\n"
            "然后先双击 cmdgo-provider.exe 或 start_proxy.cmd，再重试登录。"
        )
        return

    # 1) 发起登录
    req = urllib.request.Request(base + "/login", data=b"", headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read())
    except urllib.error.URLError as e:
        _info("无法连接本地代理：" + str(e))
        return
    if not j.get("ok"):
        _info("登录启动失败：" + str(j))
        return
    auth_url = j["authUrl"]
    _info("请在浏览器中完成 Command Code 授权：\n" + auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # 2) 轮询状态
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/login/status", timeout=10) as r:
                st = json.loads(r.read())
        except Exception as e:
            _info("查询登录状态出错：" + str(e))
            return
        if st.get("status") == "success" and st.get("hasKey"):
            _info("登录成功！Go key 已缓存，现在可以直接在 Hermes Studio 里用了。")
            return
        if st.get("status") in ("error", "cancelled"):
            _info("授权失败：" + st.get("message", "未知错误"))
            return
        time.sleep(1.5)
    _info("登录超时（10 分钟）。可重新运行本脚本重试。")


if __name__ == "__main__":
    main()
