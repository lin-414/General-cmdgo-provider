#!/usr/bin/env python3
"""
cmdgo-provider 图形界面 — 现代深色主题 + 系统托盘常驻。

用法：
  python cmdgo_gui.py
  python cmdgo_gui.py --port 8787
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
import tkinter
import urllib.request
from contextlib import redirect_stdout

# ---------------------------------------------------------------------------
# 把 cmdgo_provider 的日志重定向到 GUI
# ---------------------------------------------------------------------------
_log_buffer: list[str] = []
_log_lock = threading.Lock()


class _GUILogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        with _log_lock:
            _log_buffer.append(msg)
            # 只保留最近 500 行
            if len(_log_buffer) > 500:
                _log_buffer.pop(0)


# 在 import cmdgo_provider 之前配置日志
_handler = _GUILogHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 导入代理核心（会触发模块级初始化）
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cmdgo_provider as proxy  # noqa: E402

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
import customtkinter as ctk
from PIL import Image, ImageDraw

# 主题
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 托盘图标（简单圆形）
def _make_tray_icon(color: str = "#4CAF50", size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    # 画一个简化的 "G"
    draw.arc([size * 0.25, size * 0.2, size * 0.75, size * 0.8],
             start=30, end=320, fill="white", width=max(size // 10, 3))
    return img


class App(ctk.CTk):
    def __init__(self, port: int):
        super().__init__()

        self.port = port
        self._server = None
        self._running = False
        self._tray_icon = None
        self._tray_thread = None

        # 窗口设置
        self.title("cmdgo-provider")
        self.geometry("520x420")
        self.minsize(420, 320)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 设置窗口图标（用托盘图标）
        self._icon_img = _make_tray_icon()
        # tkinter 需要 PhotoImage
        self._tk_icon = ctk.CTkImage(self._icon_img, size=(32, 32))

        self._build_ui()
        self._start_log_poll()

        # 自动启动代理
        self.after(300, self._toggle_proxy)

    # ---- UI 构建 ----
    def _build_ui(self):
        # 顶部状态栏
        self._frame_status = ctk.CTkFrame(self, height=60, corner_radius=0)
        self._frame_status.pack(fill="x", padx=0, pady=0)
        self._frame_status.pack_propagate(False)

        self._lbl_status = ctk.CTkLabel(
            self._frame_status, text="● 已停止",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#f44336",
        )
        self._lbl_status.pack(side="left", padx=16, pady=12)

        self._lbl_info = ctk.CTkLabel(
            self._frame_status, text=f"端口 {self.port}  |  模型 —",
            font=ctk.CTkFont(size=13),
            text_color="#aaa",
        )
        self._lbl_info.pack(side="right", padx=16, pady=12)

        # 按钮栏
        self._frame_btn = ctk.CTkFrame(self, height=50, fg_color="transparent")
        self._frame_btn.pack(fill="x", padx=12, pady=(10, 4))

        self._btn_proxy = ctk.CTkButton(
            self._frame_btn, text="启动代理", width=120, height=36,
            command=self._toggle_proxy,
            fg_color="#4CAF50", hover_color="#388E3C",
        )
        self._btn_proxy.pack(side="left", padx=(0, 8))

        self._btn_login = ctk.CTkButton(
            self._frame_btn, text="OAuth 登录", width=120, height=36,
            command=self._do_login,
            fg_color="#2196F3", hover_color="#1565C0",
        )
        self._btn_login.pack(side="left", padx=(0, 8))

        self._btn_tray = ctk.CTkButton(
            self._frame_btn, text="最小化到托盘", width=120, height=36,
            command=self._minimize_to_tray,
            fg_color="#607D8B", hover_color="#455A64",
        )
        self._btn_tray.pack(side="right")

        # 日志区域
        self._txt_log = ctk.CTkTextbox(
            self, font=ctk.CTkFont(family="Consolas", size=12),
            state="disabled", wrap="word",
        )
        self._txt_log.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    # ---- 日志轮询 ----
    def _start_log_poll(self):
        self._poll_log()
        self.after(500, self._start_log_poll)

    def _poll_log(self):
        with _log_lock:
            if not _log_buffer:
                return
            lines = list(_log_buffer)
            _log_buffer.clear()
        self._txt_log.configure(state="normal")
        for line in lines:
            self._txt_log.insert("end", line + "\n")
        self._txt_log.see("end")
        self._txt_log.configure(state="disabled")

    # ---- 代理控制 ----
    def _toggle_proxy(self):
        if self._running:
            self._stop_proxy()
        else:
            self._start_proxy()

    def _start_proxy(self):
        if self._running:
            return
        proxy.PORT = self.port
        try:
            self._server = proxy.start_server(block=False)
        except OSError as e:
            proxy.log("启动失败: %s", e)
            return
        self._running = True
        self._btn_proxy.configure(text="停止代理", fg_color="#f44336", hover_color="#c62828")
        self._lbl_status.configure(text="● 运行中", text_color="#4CAF50")
        self._update_info()

    def _stop_proxy(self):
        if not self._running:
            return
        if self._server:
            threading.Thread(target=self._server.shutdown, daemon=True).start()
            self._server = None
        # 重置模块级 _server 变量以便重启
        proxy._server = None
        self._running = False
        self._btn_proxy.configure(text="启动代理", fg_color="#4CAF50", hover_color="#388E3C")
        self._lbl_status.configure(text="● 已停止", text_color="#f44336")
        self._lbl_info.configure(text=f"端口 {self.port}  |  模型 —")

    def _update_info(self):
        if not self._running:
            return
        model_count = len(proxy.model_cache.get("models", []))
        self._lbl_info.configure(text=f"端口 {self.port}  |  模型 {model_count} 个")
        # 定期刷新
        self.after(10_000, self._update_info)

    # ---- OAuth 登录 ----
    def _do_login(self):
        if not self._running:
            self._start_proxy()
            if not self._running:
                return

        self._btn_login.configure(state="disabled", text="登录中…")
        threading.Thread(target=self._login_thread, daemon=True).start()

    def _login_thread(self):
        base = f"http://127.0.0.1:{self.port}"
        try:
            req = urllib.request.Request(base + "/login", data=b"",
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.loads(r.read())
        except Exception as e:
            proxy.log("登录请求失败: %s", e)
            self.after(0, lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))
            return

        if not j.get("ok"):
            proxy.log("登录启动失败: %s", j)
            self.after(0, lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))
            return

        auth_url = j["authUrl"]
        proxy.log("请在浏览器中完成授权: %s", auth_url)
        import webbrowser
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        # 轮询
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/login/status", timeout=10) as r:
                    st = json.loads(r.read())
            except Exception:
                time.sleep(2)
                continue
            if st.get("status") == "success" and st.get("hasKey"):
                proxy.log("登录成功！Go key 已缓存")
                break
            if st.get("status") == "error":
                proxy.log("授权失败: %s", st.get("message", "未知错误"))
                break
            time.sleep(1.5)
        else:
            proxy.log("登录超时（10 分钟）")

        self.after(0, lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))

    # ---- 系统托盘 ----
    def _minimize_to_tray(self):
        self.withdraw()
        if self._tray_icon is None:
            self._setup_tray()

    def _setup_tray(self):
        import pystray

        def on_show(icon, item):
            self.after(0, self._restore_from_tray)

        def on_quit(icon, item):
            icon.stop()
            self.after(0, self._quit_app)

        def on_toggle(icon, item):
            self.after(0, self._toggle_proxy)
            # 更新图标颜色
            time.sleep(0.3)
            color = "#4CAF50" if self._running else "#f44336"
            icon.icon = _make_tray_icon(color)

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", on_show, default=True),
            pystray.MenuItem("启动/停止代理", on_toggle),
            pystray.MenuItem("退出", on_quit),
        )

        color = "#4CAF50" if self._running else "#f44336"
        self._tray_icon = pystray.Icon(
            "cmdgo-provider", _make_tray_icon(color),
            "cmdgo-provider", menu,
        )
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _restore_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    # ---- 关闭 ----
    def _on_close(self):
        self._minimize_to_tray()

    def _quit_app(self):
        if self._running:
            self._stop_proxy()
        if self._tray_icon:
            self._tray_icon.stop()
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="cmdgo-provider 图形界面")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    args = ap.parse_args()

    app = App(port=args.port)
    app.mainloop()


if __name__ == "__main__":
    main()
