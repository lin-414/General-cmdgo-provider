#!/usr/bin/env python3
"""
General-cmdgo-provider 图形界面 — 现代深色主题 + 系统托盘常驻。

用法：
  python cmdgo_gui.py
  python cmdgo_gui.py --port 8787
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
import tkinter
import urllib.request
import webbrowser

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
import appicon  # noqa: E402

# ---------------------------------------------------------------------------
# 日志落盘（轮转）：GUI 运行期间同步写进用户数据目录，便于事后排查
# ---------------------------------------------------------------------------
try:
    _log_dir = os.path.join(proxy._data_dir(), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "app.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
    logging.root.addHandler(_file_handler)
except Exception:
    pass  # 日志目录不可写不影响运行

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
import customtkinter as ctk  # noqa: E402  # 必须在日志配置之后导入

# 主题
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _system_font_family(root=None) -> str:
    """读取当前系统的默认 UI 字体（如 Windows 的 Microsoft YaHei UI / Segoe UI）。

    customtkinter 默认用自带的 Roboto，用户要求用系统默认，故动态读取 Tk 的
    TkDefaultFont 字体族名，任何系统上都能匹配。
    """
    try:
        import tkinter.font as tkfont
        root = root or tkinter._get_default_root()
        return tkfont.nametofont("TkDefaultFont").actual("family")
    except Exception:
        return "Segoe UI"

def _make_tray_icon(color: str = appicon.BASE_COLOR, size: int = 64):
    """托盘图标 — 设计统一收敛到 appicon.py（与窗口/任务栏/EXE 图标同源）。"""
    return appicon.make_icon_image(color, size)


# ---------------------------------------------------------------------------
# 开机自启（Windows：HKCU Run 键；配合 --minimized 启动即常驻托盘）
# ---------------------------------------------------------------------------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "General-cmdgo-provider"


def autostart_supported() -> bool:
    return os.name == "nt"


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _RUN_VALUE)
        return True
    except OSError:
        return False


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    # 源码运行：用 pythonw 避免开机时闪控制台窗口
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(enabled: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        proxy.log("开机自启设置失败: %s", e)
        return False


def _version_tuple(v: str) -> tuple:
    """'v0.10.2' -> (0, 10, 2)；解析失败返回空元组（视为不比较）。"""
    try:
        return tuple(int(x) for x in v.strip().lstrip("vV").split("."))
    except Exception:
        return ()


def _fmt_ctx(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1_000:
        return f"{n // 1000}K"
    return str(n)


def _fmt_tokens(n) -> str:
    if not isinstance(n, (int, float)):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _num(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) else "—"


def _reset_text(ms) -> str:
    """额度窗口重置时间：剩余时长的可读描述。"""
    if not isinstance(ms, (int, float)):
        return ""
    remain = ms / 1000 - time.time()
    if remain <= 0:
        return "即将重置"
    if remain < 3600:
        return f"{int(remain / 60) + 1} 分钟后重置"
    if remain < 86400:
        return f"{remain / 3600:.1f} 小时后重置"
    return f"{remain / 86400:.1f} 天后重置"


class App(ctk.CTk):
    def __init__(self, port: int, start_minimized: bool = False):
        super().__init__()

        # 让全局主题字体跟随系统默认（按钮/标签等未显式指定 font 的控件都会继承）
        try:
            _sys = _system_font_family(self)
            ctk.ThemeManager.theme["CTkFont"]["family"] = _sys
        except Exception:
            pass

        self.port = port
        self._server = None
        self._running = False
        self._started_at = None      # 代理启动时间（概览页运行时长）
        self._tray_icon = None
        self._tray_thread = None
        self._accounts = []          # 账号池快照（来自 /account/list）
        self._acct_row_widgets = []  # 账号列表行控件引用
        self._acct_refreshing = False  # 防止并发账号刷新
        # 跨线程安全调度：所有非主线程对 UI 的更新都入队，由主循环轮询执行。
        self._dispatch_q: "queue.Queue" = queue.Queue()
        self._dispatch_started = False

        # 窗口设置
        self.title("General-cmdgo-provider")
        self.geometry("900x680")
        self.minsize(820, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 窗口/任务栏图标与托盘同款（绿色圆形 G），不再使用 assets/icon.ico 旧设计
        self._tk_icon = self._load_window_icon()

        self._build_ui()
        self._start_log_poll()
        self._start_dispatch()

        # 自动启动代理
        self.after(300, self._toggle_proxy)
        # 开机自启传入 --minimized：先渲染一拍再收进托盘
        if start_minimized:
            self.after(600, self._minimize_to_tray)
        # 检查新版本（后台线程，静默失败）
        threading.Thread(target=self._check_update_thread, daemon=True).start()

    # ---- 跨线程安全调度 ----
    def _start_dispatch(self):
        """启动主循环轮询器，专门处理从任何线程投递过来的 UI 更新。

        tkinter 的 ``after()`` 只能从主线程调用；若从后台线程调用会破坏 Tcl
        解释器，导致界面长时间后"未响应"。因此所有跨线程 UI 更新统一走队列。
        """
        if self._dispatch_started:
            return
        self._dispatch_started = True
        self.after(50, self._drain_dispatch)

    def _dispatch(self, fn, *args, delay=0, **kwargs):
        """线程安全地把一个可调用对象投递给主循环（可在任何线程调用）。

        delay>0 时，投递给主循环后再用 after 延迟触发（只在主线程内调用 after）。
        """
        if delay and delay > 0:
            self._dispatch_q.put((self._after_wrap, (fn, args, kwargs, delay), {}))
        else:
            self._dispatch_q.put((fn, args, kwargs))

    def _after_wrap(self, fn, args, kwargs, delay):
        """在主线程内用 after 延迟触发某个可调用对象。"""
        self.after(delay, lambda: self._dispatch(fn, *args, **kwargs))

    def _drain_dispatch(self):
        while True:
            try:
                fn, args, kwargs = self._dispatch_q.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args, **kwargs)
            except Exception:
                # 不能静默吞掉：回调里的 bug 会在这里消失得无影无踪。
                logging.getLogger(__name__).exception("dispatch 回调执行失败")
        if self._dispatch_started:
            self.after(50, self._drain_dispatch)

    def _load_window_icon(self):
        """窗口/任务栏图标：与托盘同款（绿色圆形 G）。

        注意：customtkinter 会在初始化 200ms 后用自带的蓝色图标覆盖窗口图标
        （_windows_set_titlebar_icon），除非用户调用过 iconbitmap()。iconphoto
        不在其豁免名单里（会被覆盖成蓝色）——因此必须走 CTk 的 iconbitmap()。
        """
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "assets", "icon.ico")
            if os.path.isfile(ico):
                self.iconbitmap(ico)
                return
        except Exception:
            pass
        # 没有 .ico 时（理论不发生，assets 已随包打包）：标记已接管，再用动态渲染兜底
        try:
            self.iconbitmap()  # 置 _iconbitmap_method_called，阻断 CTk 的覆盖
            from PIL import ImageTk
            self.iconphoto(True, ImageTk.PhotoImage(appicon.make_icon_image(size=256)))
        except Exception:
            pass

    # ---- UI 构建 ----
    def _build_ui(self):
        # 顶部状态栏（所有标签页共享）
        self._frame_status = ctk.CTkFrame(self, height=60, corner_radius=0)
        self._frame_status.pack(fill="x", padx=0, pady=0)
        self._frame_status.pack_propagate(False)

        self._lbl_status = ctk.CTkLabel(
            self._frame_status, text="● 已停止",
            font=ctk.CTkFont(family=_system_font_family(), size=18, weight="bold"),
            text_color="#f44336",
        )
        self._lbl_status.pack(side="left", padx=16, pady=12)

        self._lbl_info = ctk.CTkLabel(
            self._frame_status, text=f"端口 {self.port}  |  模型 —",
            font=ctk.CTkFont(family=_system_font_family(), size=13),
            text_color="#aaa",
        )
        self._lbl_info.pack(side="right", padx=16, pady=12)

        # 更新提示（有新版本时显示，点击打开发布页）
        self._update_url = ""
        self._lbl_update = ctk.CTkLabel(
            self._frame_status, text="", cursor="hand2",
            font=ctk.CTkFont(family=_system_font_family(), size=13),
        )
        self._lbl_update.pack(side="right", padx=(0, 12))
        self._lbl_update.bind(
            "<Button-1>", lambda _e: webbrowser.open(self._update_url) if self._update_url else None)

        # 开机自启开关（仅 Windows）
        self._chk_autostart = ctk.CTkCheckBox(
            self._frame_status, text="开机自启", width=92,
            font=ctk.CTkFont(family=_system_font_family(), size=12),
            command=self._toggle_autostart,
        )
        if autostart_supported():
            self._chk_autostart.pack(side="right", padx=(0, 4))
            if autostart_enabled():
                self._chk_autostart.select()
        else:
            self._chk_autostart.configure(state="disabled")

        # 标签页：概览 / 账号 / 用量与额度 / 模型型号 / 实时日志
        self._tabs = ctk.CTkTabview(self)
        self._tabs.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self._build_overview_tab(self._tabs.add("概览"))
        self._build_accounts_tab(self._tabs.add("账号"))
        self._build_usage_tab(self._tabs.add("用量与额度"))
        self._build_models_tab(self._tabs.add("模型型号"))
        self._build_logs_tab(self._tabs.add("实时日志"))

    # ---- 概览标签页 ----
    def _build_overview_tab(self, parent):
        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.pack(fill="x", pady=(2, 6))
        for col in range(4):
            cards.grid_columnconfigure(col, weight=1, uniform="ov")
        self._ov_labels = {}
        specs = [
            ("engine", "代理状态", "● 已停止", "#f44336"),
            ("endpoint", "监听地址", f"127.0.0.1:{self.port}", None),
            ("accounts", "账号", "—", None),
            ("models", "可用模型", "—", None),
        ]
        for col, (key, title, value, color) in enumerate(specs):
            card = ctk.CTkFrame(cards, corner_radius=10)
            card.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0 if col == 3 else 4), sticky="nsew")
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(family=_system_font_family(), size=12),
                         text_color="#8b949e").pack(anchor="w", padx=12, pady=(10, 0))
            lbl = ctk.CTkLabel(card, text=value, text_color=color or "#e6edf3",
                               font=ctk.CTkFont(family=_system_font_family(), size=19, weight="bold"))
            lbl.pack(anchor="w", padx=12, pady=(0, 12))
            self._ov_labels[key] = lbl

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", pady=(0, 6))
        self._btn_proxy = ctk.CTkButton(
            btns, text="启动代理", width=120, height=34, command=self._toggle_proxy,
            fg_color="#4CAF50", hover_color="#388E3C",
        )
        self._btn_proxy.pack(side="left", padx=(0, 8))
        self._btn_login = ctk.CTkButton(
            btns, text="OAuth 登录", width=120, height=34, command=self._do_login,
            fg_color="#2196F3", hover_color="#1565C0",
        )
        self._btn_login.pack(side="left", padx=(0, 8))
        self._btn_tray = ctk.CTkButton(
            btns, text="最小化到托盘", width=120, height=34, command=self._minimize_to_tray,
            fg_color="#607D8B", hover_color="#455A64",
        )
        self._btn_tray.pack(side="left")
        self._btn_web = ctk.CTkButton(
            btns, text="网页面板", width=90, height=34, command=self._open_web_panel,
            fg_color="#607D8B", hover_color="#455A64",
        )
        self._btn_web.pack(side="right")

        info = ctk.CTkFrame(parent, corner_radius=10)
        info.pack(fill="both", expand=True)
        self._lbl_ov_detail = ctk.CTkLabel(
            info, text="—", justify="left", anchor="w",
            font=ctk.CTkFont(family=_system_font_family(), size=12), text_color="#8b949e",
        )
        self._lbl_ov_detail.pack(fill="x", padx=12, pady=12)

    # ---- 账号标签页 ----
    def _build_accounts_tab(self, parent):
        self._acct_header = ctk.CTkFrame(parent, fg_color="transparent")
        self._acct_header.pack(fill="x", pady=(2, 4))
        ctk.CTkLabel(self._acct_header, text="账号池（多号轮询）",
                     font=ctk.CTkFont(family=_system_font_family(), size=13, weight="bold"),
                     text_color="#aaa").pack(side="left")
        self._lbl_acct_count = ctk.CTkLabel(
            self._acct_header, text="0 个账号",
            font=ctk.CTkFont(family=_system_font_family(), size=12), text_color="#4CAF50",
        )
        self._lbl_acct_count.pack(side="right")
        _btn_small_font = ctk.CTkFont(family=_system_font_family(), size=11)
        self._btn_export = ctk.CTkButton(self._acct_header, text="导出", width=44, height=22,
                                         font=_btn_small_font, fg_color="#607D8B", hover_color="#455A64",
                                         command=self._export_accounts)
        self._btn_export.pack(side="right", padx=(4, 10))
        self._btn_import = ctk.CTkButton(self._acct_header, text="导入", width=44, height=22,
                                         font=_btn_small_font, fg_color="#607D8B", hover_color="#455A64",
                                         command=self._import_accounts)
        self._btn_import.pack(side="right", padx=4)
        self._acct_list = ctk.CTkScrollableFrame(parent, label_text="")
        self._acct_list.pack(fill="both", expand=True, pady=(2, 0))
        self._refresh_accounts()

    # ---- 用量与额度标签页 ----
    def _build_usage_tab(self, parent):
        quota = ctk.CTkFrame(parent, fg_color="transparent")
        quota.pack(fill="x", pady=(2, 6))
        for col in range(3):
            quota.grid_columnconfigure(col, weight=1, uniform="quota")
        self._quota_widgets = {}
        for col, (key, title) in enumerate([("fiveHour", "5 小时窗口"), ("weekly", "本周窗口"),
                                            ("monthly", "月度额度")]):
            card = ctk.CTkFrame(quota, corner_radius=10)
            card.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0 if col == 2 else 4), sticky="nsew")
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(family=_system_font_family(), size=12),
                         text_color="#8b949e").pack(anchor="w", padx=12, pady=(10, 0))
            value = ctk.CTkLabel(card, text="—",
                                 font=ctk.CTkFont(family=_system_font_family(), size=19, weight="bold"))
            value.pack(anchor="w", padx=12)
            bar = ctk.CTkProgressBar(card, height=8)
            bar.set(0)
            bar.pack(fill="x", padx=12, pady=(6, 2))
            sub = ctk.CTkLabel(card, text="", font=ctk.CTkFont(family=_system_font_family(), size=11),
                               text_color="#8b949e")
            sub.pack(anchor="w", padx=12, pady=(0, 10))
            self._quota_widgets[key] = {"value": value, "bar": bar, "sub": sub}

        totals = ctk.CTkFrame(parent, corner_radius=10)
        totals.pack(fill="x", pady=(0, 6))
        self._lbl_usage_totals = ctk.CTkLabel(
            totals, text="用量数据加载中…", justify="left", anchor="w",
            font=ctk.CTkFont(family=_system_font_family(), size=12),
        )
        self._lbl_usage_totals.pack(fill="x", padx=12, pady=10)

        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", pady=(2, 2))
        ctk.CTkLabel(head, text="各账号用量（本地累计）",
                     font=ctk.CTkFont(family=_system_font_family(), size=12, weight="bold"),
                     text_color="#aaa").pack(side="left")
        self._btn_usage_refresh = ctk.CTkButton(head, text="刷新", width=52, height=24,
                                               font=ctk.CTkFont(family=_system_font_family(), size=11),
                                               fg_color="#607D8B", hover_color="#455A64",
                                               command=lambda: self._refresh_usage(force=True))
        self._btn_usage_refresh.pack(side="right")
        self._lbl_usage_status = ctk.CTkLabel(head, text="",
                                              font=ctk.CTkFont(family=_system_font_family(), size=11),
                                              text_color="#8b949e")
        self._lbl_usage_status.pack(side="right", padx=8)
        self._usage_list = ctk.CTkScrollableFrame(parent)
        self._usage_list.pack(fill="both", expand=True)
        self._usage_rows = []
        self._refresh_usage()

    # ---- 模型型号标签页 ----
    def _build_models_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", pady=(2, 4))
        self._ent_model = ctk.CTkEntry(
            head, placeholder_text="搜索模型 id / 名称…",
            font=ctk.CTkFont(family=_system_font_family(), size=12),
        )
        self._ent_model.pack(side="left", fill="x", expand=True)
        self._ent_model.bind("<KeyRelease>", lambda _e: self._render_models())
        self._lbl_model_count = ctk.CTkLabel(head, text="—",
                                             font=ctk.CTkFont(family=_system_font_family(), size=11),
                                             text_color="#8b949e")
        self._lbl_model_count.pack(side="right", padx=(8, 0))
        ctk.CTkLabel(head, text="点击模型 id 即复制",
                     font=ctk.CTkFont(family=_system_font_family(), size=11),
                     text_color="#8b949e").pack(side="right", padx=(8, 0))
        self._model_list = ctk.CTkScrollableFrame(parent)
        self._model_list.pack(fill="both", expand=True, pady=(2, 0))
        self._model_rows = []
        self._models_sig = None
        self._render_models()

    # ---- 实时日志标签页 ----
    def _build_logs_tab(self, parent):
        head = ctk.CTkFrame(parent, fg_color="transparent")
        head.pack(fill="x", pady=(2, 4))
        try:
            log_path = os.path.join(proxy._data_dir(), "logs", "app.log")
        except Exception:
            log_path = ""
        ctk.CTkLabel(head, text=f"日志文件：{log_path}",
                     font=ctk.CTkFont(family=_system_font_family(), size=11),
                     text_color="#8b949e").pack(side="left")
        ctk.CTkButton(head, text="清空显示", width=64, height=24,
                      font=ctk.CTkFont(family=_system_font_family(), size=11),
                      fg_color="#607D8B", hover_color="#455A64",
                      command=self._clear_log).pack(side="right")
        self._txt_log = ctk.CTkTextbox(
            # 日志含中文：用系统默认字体，避免等宽字体对中文回退造成混排不一致
            parent, font=ctk.CTkFont(family=_system_font_family(), size=12),
            state="disabled", wrap="word",
        )
        self._txt_log.pack(fill="both", expand=True)

    def _clear_log(self):
        self._txt_log.configure(state="normal")
        self._txt_log.delete("1.0", "end")
        self._txt_log.configure(state="disabled")

    def _open_web_panel(self):
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _refresh_usage(self, force: bool = False):
        """拉取用量/额度（后台线程），渲染后再安排下一次刷新。"""
        threading.Thread(target=self._refresh_usage_thread, args=(force,), daemon=True).start()

    def _refresh_usage_thread(self, force: bool):
        try:
            url = f"http://127.0.0.1:{self.port}/usage/overview" + ("?refresh=1" if force else "")
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read())
        except Exception as e:
            data = {"ok": False, "error": f"本地代理未运行或不可达：{e}"}
        self._dispatch(self._render_usage, data)
        self._dispatch(self._refresh_usage, delay=60_000)

    def _render_usage(self, data: dict):
        if not isinstance(data, dict):
            return
        usage = data.get("usage") or {}
        credits = data.get("credits") or {}
        windows = credits.get("windowLimits") or {}

        for key in ("fiveHour", "weekly"):
            widgets = self._quota_widgets[key]
            win = windows.get(key) or {}
            used, cap = win.get("used"), win.get("cap")
            if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap:
                widgets["value"].configure(text=f"{used / cap * 100:.1f}%")
                widgets["bar"].set(min(max(used / cap, 0.0), 1.0))
                reset = _reset_text(win.get("resetAt"))
                widgets["sub"].configure(text=f"{used:g} / {cap:g}" + (f" · {reset}" if reset else ""))
            else:
                widgets["value"].configure(text="—")
                widgets["bar"].set(0)
                widgets["sub"].configure(text="")

        # 月度额度：上游给的是「已用」(summary.totalMonthlyCredits) 和「剩余」
        # (credits.monthlyCredits)，上限 = 已用 + 剩余（与官方页面口径一致）。
        monthly_used = usage.get("totalMonthlyCredits")
        monthly_remaining = (credits.get("credits") or {}).get("monthlyCredits")
        used_val = monthly_used if isinstance(monthly_used, (int, float)) else 0.0
        remain_val = monthly_remaining if isinstance(monthly_remaining, (int, float)) else 0.0
        cap_val = used_val + remain_val
        widgets = self._quota_widgets["monthly"]
        if cap_val > 0:
            widgets["value"].configure(text=f"{used_val / cap_val * 100:.1f}%")
            widgets["bar"].set(min(max(used_val / cap_val, 0.0), 1.0))
            widgets["sub"].configure(
                text=f"已用 {used_val:.2f} / {cap_val:.2f} credits · 剩余 {remain_val:.2f}")
        else:
            widgets["value"].configure(text="—")
            widgets["bar"].set(0)
            widgets["sub"].configure(text="按量计费（无限额）")

        if data.get("ok"):
            total = usage.get("totalCount")
            completed = usage.get("completedCount")
            failed = usage.get("failedCount")
            rate = usage.get("successRate")
            self._lbl_usage_totals.configure(
                text=(f"总请求 {_num(total)}（成功 {_num(completed)} · 失败 {_num(failed)} · 成功率 {rate}%）\n"
                      f"Tokens：输入 {_fmt_tokens(usage.get('totalTokensIn'))} · "
                      f"输出 {_fmt_tokens(usage.get('totalTokensOut'))} · "
                      f"合计 {_fmt_tokens(usage.get('totalTokens'))}\n"
                      f"总成本 {usage.get('totalCost') if usage.get('totalCost') is not None else '—'} credits · "
                      f"周期 {usage.get('periodBasis') or '—'}"),
                text_color="#e6edf3",
            )
            at = data.get("at")
            stamp = time.strftime("%H:%M:%S", time.localtime(at / 1000)) if at else time.strftime("%H:%M:%S")
            self._lbl_usage_status.configure(text=f"更新于 {stamp}", text_color="#8b949e")
        else:
            self._lbl_usage_totals.configure(text=f"无法获取用量：{data.get('error') or '未知错误'}",
                                             text_color="#d29922")
            self._lbl_usage_status.configure(text="", text_color="#8b949e")

        self._render_usage_stats(data.get("local") or {})

    def _render_usage_stats(self, local: dict):
        for w in self._usage_rows:
            w.destroy()
        self._usage_rows = []
        accounts = local.get("accounts") or []
        if not accounts:
            hint = ctk.CTkLabel(self._usage_list, text="暂无账号",
                                font=ctk.CTkFont(family=_system_font_family(), size=12), text_color="#666")
            hint.pack(pady=8)
            self._usage_rows.append(hint)
            return
        for acc in accounts:
            row = ctk.CTkFrame(self._usage_list, fg_color="#1f262f", corner_radius=6)
            row.pack(fill="x", pady=2, padx=2)
            name = acc.get("displayName") or acc.get("userName") or acc.get("id")
            ctk.CTkLabel(row, text=name,
                         font=ctk.CTkFont(family=_system_font_family(), size=12),
                         text_color="#e6edf3").pack(side="left", padx=(10, 8), pady=5)
            ctk.CTkLabel(row, text=f"↑ {_fmt_tokens(acc.get('tokensIn'))}  ↓ {_fmt_tokens(acc.get('tokensOut'))}",
                         font=ctk.CTkFont(family=_system_font_family(), size=11),
                         text_color="#8b949e").pack(side="right", padx=(0, 10))
            ctk.CTkLabel(row, text=f"成功 {acc.get('okCount') or 0} · 失败 {acc.get('errCount') or 0}",
                         font=ctk.CTkFont(family=_system_font_family(), size=11),
                         text_color="#8b949e").pack(side="right", padx=(0, 12))
            self._usage_rows.extend([row])

    def _copy_model_id(self, model_id: str):
        """把模型 id 复制到剪贴板（点击模型行触发）。"""
        try:
            self.clipboard_clear()
            self.clipboard_append(model_id)
            proxy.log("已复制模型 id：%s", model_id)
        except Exception as e:
            proxy.log("复制模型 id 失败: %s", e)

    def _render_models(self):
        models = proxy.model_cache.get("models", [])
        query = (self._ent_model.get() or "").strip().lower()
        sig = (len(models), models[0]["id"] if models else "", query)
        if sig == self._models_sig:
            return  # 模型目录与搜索词都没变，跳过重建（避免每 10 秒重建上百个控件）
        self._models_sig = sig
        shown = [m for m in models
                 if not query or query in m["id"].lower() or query in (m.get("name") or "").lower()]
        for w in self._model_rows:
            w.destroy()
        self._model_rows = []
        self._lbl_model_count.configure(text=f"{len(shown)} / {len(models)} 个")
        if not shown:
            hint = ctk.CTkLabel(self._model_list, text="没有匹配的模型",
                                font=ctk.CTkFont(family=_system_font_family(), size=12), text_color="#666")
            hint.pack(pady=8)
            self._model_rows.append(hint)
            return
        for m in shown:
            row = ctk.CTkFrame(self._model_list, fg_color="#1f262f", corner_radius=6)
            row.pack(fill="x", pady=1, padx=2)
            ctx = m.get("context_length")
            if isinstance(ctx, int):
                ctk.CTkLabel(row, text=_fmt_ctx(ctx),
                             font=ctk.CTkFont(family=_system_font_family(), size=11),
                             text_color="#58a6ff").pack(side="right", padx=(6, 10), pady=4)
            ctk.CTkLabel(row, text=m.get("name") or "",
                         font=ctk.CTkFont(family=_system_font_family(), size=11),
                         text_color="#8b949e").pack(side="right", padx=(6, 6))
            mid = m["id"]
            lbl_id = ctk.CTkLabel(row, text=mid, cursor="hand2",
                                  font=ctk.CTkFont(family="Consolas", size=12),
                                  text_color="#e6edf3", anchor="w")
            lbl_id.pack(side="left", padx=(10, 6), pady=4)
            # 点击模型 id（或整行）复制到剪贴板
            lbl_id.bind("<Button-1>", lambda _e, i=mid: self._copy_model_id(i))
            row.bind("<Button-1>", lambda _e, i=mid: self._copy_model_id(i))
            self._model_rows.append(row)

    # ---- 账号池 ----
    def _refresh_accounts(self):
        """拉取账号池并渲染；在后台线程执行，避免阻塞主线程。"""
        if self._acct_refreshing:
            return
        self._acct_refreshing = True
        threading.Thread(target=self._refresh_accounts_thread, daemon=True).start()

    def _refresh_accounts_thread(self):
        try:
            base = f"http://127.0.0.1:{self.port}"
            with urllib.request.urlopen(base + "/account/list", timeout=4) as r:
                data = json.loads(r.read())
            self._accounts = data.get("accounts", [])
        except Exception:
            # 服务器未就绪/已停止：保留上次快照，避免账号面板每 5 秒闪烁清空。
            pass
        finally:
            self._acct_refreshing = False
        # 线程安全投递到主循环执行（不可直接调用 self.after/set 控件）。
        self._dispatch(self._render_accounts)
        # 定期刷新账号状态
        self._dispatch(self._refresh_accounts, delay=5_000)

    def _render_accounts(self):
        for w in self._acct_row_widgets:
            w.destroy()
        self._acct_row_widgets = []
        count = len(self._accounts)
        self._lbl_acct_count.configure(text=f"{count} 个账号")
        if not self._accounts:
            hint = ctk.CTkLabel(self._acct_list, text="暂无账号，点击「OAuth 登录」添加多个账号",
                                font=ctk.CTkFont(family=_system_font_family(), size=12), text_color="#666")
            hint.pack(pady=8)
            self._acct_row_widgets.append(hint)
            return
        for acc in self._accounts:
            row = ctk.CTkFrame(self._acct_list, fg_color="#1f262f", corner_radius=8)
            row.pack(fill="x", pady=2, padx=2)
            # 状态点 + 名称
            status = "●" if acc.get("enabled") and not acc.get("cooling") else ("◐" if acc.get("cooling") else "○")
            color = "#4CAF50" if acc.get("enabled") and not acc.get("cooling") else ("#d29922" if acc.get("cooling") else "#f44336")
            name = acc.get("displayName") or acc.get("userName") or acc.get("keyName") or acc.get("id")
            lbl = ctk.CTkLabel(row, text=f"{status} {name}", cursor="hand2",
                               font=ctk.CTkFont(family=_system_font_family(), size=12), text_color=color)
            lbl.pack(side="left", padx=(10, 4), pady=6)
            lbl.bind("<Button-1>", lambda _e, i=acc.get("id", ""): self._log_account_details(i))
            # 用量统计（累计）：成功/失败次数 + token 总量
            stats = []
            if acc.get("okCount"):
                stats.append(f"成功 {acc['okCount']}")
            if acc.get("errCount"):
                stats.append(f"失败 {acc['errCount']}")
            tok = (acc.get("tokensIn") or 0) + (acc.get("tokensOut") or 0)
            if tok:
                stats.append(f"{tok / 1000:.1f}k tokens" if tok >= 1000 else f"{tok} tokens")
            if stats:
                lbl_stats = ctk.CTkLabel(row, text=" · ".join(stats),
                                         font=ctk.CTkFont(family=_system_font_family(), size=11),
                                         text_color="#666")
                lbl_stats.pack(side="left", padx=(0, 4), pady=6)
                self._acct_row_widgets.append(lbl_stats)
            # 小按钮：测试 + 启用/禁用 + 删除
            a_id = acc.get("id", "")
            btn_test = ctk.CTkButton(row, text="测试", width=44, height=24,
                                     font=ctk.CTkFont(family=_system_font_family(), size=11),
                                     fg_color="#2196F3", hover_color="#1565C0",
                                     command=lambda i=a_id: self._test_account(i))
            btn_test.pack(side="right", padx=2, pady=4)
            btn_toggle = ctk.CTkButton(row, text="停用" if acc.get("enabled") else "启用", width=52, height=24,
                                       font=ctk.CTkFont(family=_system_font_family(), size=11),
                                       fg_color="#607D8B", hover_color="#455A64",
                                       command=lambda i=a_id, e=not acc.get("enabled"): self._toggle_account(i, e))
            btn_toggle.pack(side="right", padx=(2, 4), pady=4)
            btn_del = ctk.CTkButton(row, text="删除", width=44, height=24,
                                    font=ctk.CTkFont(family=_system_font_family(), size=11),
                                    fg_color="#f44336", hover_color="#c62828",
                                    command=lambda i=a_id: self._remove_account(i))
            btn_del.pack(side="right", padx=2, pady=4)
            self._acct_row_widgets.extend([row, lbl, btn_test, btn_toggle, btn_del])

    def _toggle_account(self, a_id: str, enabled: bool):
        # 在后台线程执行，避免阻塞主线程（否则界面会卡住）。
        threading.Thread(target=self._toggle_account_thread, args=(a_id, enabled), daemon=True).start()

    def _toggle_account_thread(self, a_id: str, enabled: bool):
        try:
            base = f"http://127.0.0.1:{self.port}"
            req = urllib.request.Request(base + "/account/toggle",
                                         data=json.dumps({"id": a_id, "enabled": enabled}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=4) as r:
                code = r.status
                r.read()
            if code not in (200, 404):
                proxy.log("账号切换失败（HTTP %s）", code)
        except Exception as e:
            proxy.log("账号切换失败: %s", e)
        self._dispatch(self._refresh_accounts)

    def _remove_account(self, a_id: str):
        # 在后台线程执行，避免阻塞主线程（否则界面会卡住）。
        threading.Thread(target=self._remove_account_thread, args=(a_id,), daemon=True).start()

    def _remove_account_thread(self, a_id: str):
        try:
            base = f"http://127.0.0.1:{self.port}"
            req = urllib.request.Request(base + "/account/remove",
                                         data=json.dumps({"id": a_id}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=4) as r:
                code = r.status
                r.read()
            if code == 404:
                # 账号已经不在池里（可能刚才已被删除过），不算失败。
                proxy.log("账号 %s 已不存在（可能已删除）", a_id)
            elif code != 200:
                proxy.log("账号删除失败（HTTP %s）", code)
            else:
                proxy.log("已删除账号 %s", a_id)
        except Exception as e:
            proxy.log("账号删除失败: %s", e)
        self._dispatch(self._refresh_accounts)

    # ---- 账号测试 / 详情 ----
    def _test_account(self, a_id: str):
        # 迷你请求可能要几秒，放后台线程执行
        threading.Thread(target=self._test_account_thread, args=(a_id,), daemon=True).start()

    def _test_account_thread(self, a_id: str):
        proxy.log("正在测试账号 %s …", a_id)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/account/test",
                                         data=json.dumps({"id": a_id}).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.loads(r.read())
            if res.get("ok"):
                proxy.log("✅ 账号 %s 测试通过（%sms）", a_id, res.get("latencyMs"))
            else:
                proxy.log("❌ 账号 %s 测试失败（HTTP %s）：%s", a_id, res.get("status"), res.get("message"))
        except Exception as e:
            proxy.log("账号 %s 测试请求失败: %s", a_id, e)

    def _log_account_details(self, a_id: str):
        acc = next((a for a in self._accounts if a.get("id") == a_id), None)
        if not acc:
            return
        proxy.log("账号详情 %s：启用=%s 冷却中=%s | 成功 %s 次 / 失败 %s 次 | tokens %s/%s | lastError=%s",
                  acc.get("id"), acc.get("enabled"), acc.get("cooling"),
                  acc.get("okCount"), acc.get("errCount"),
                  acc.get("tokensIn"), acc.get("tokensOut"), acc.get("lastError"))

    # ---- 账号导出/导入 ----
    def _export_accounts(self):
        from tkinter import filedialog, simpledialog
        passphrase = simpledialog.askstring("导出账号", "设置导出口令（导入时需要，请牢记）:",
                                            parent=self, show="*")
        if not passphrase:
            return
        confirm = simpledialog.askstring("导出账号", "请再输入一遍口令确认:", parent=self, show="*")
        if confirm is None:
            return
        if confirm != passphrase:
            proxy.log("导出失败：两次输入的口令不一致")
            return
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".cmdgo", initialfile="cmdgo-accounts.cmdgo",
            filetypes=[("cmdgo 账号导出", "*.cmdgo"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            data = proxy.pool.export_accounts(passphrase)
            with open(path, "wb") as f:
                f.write(data)
            proxy.log("已导出 %d 个账号 -> %s（请妥善保管口令与文件）", proxy.pool.size, path)
        except Exception as e:
            proxy.log("导出失败: %s", e)

    def _import_accounts(self):
        from tkinter import filedialog, simpledialog
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("cmdgo 账号导出", "*.cmdgo"), ("所有文件", "*.*")])
        if not path:
            return
        passphrase = simpledialog.askstring("导入账号", "输入导出时设置的口令:", parent=self, show="*")
        if passphrase is None:
            return
        try:
            with open(path, "rb") as f:
                payload = f.read()
            added = proxy.pool.import_accounts(payload, passphrase)
            proxy.log("导入完成：新增 %d 个账号（已存在的 key 自动跳过）", added)
        except Exception as e:
            proxy.log("导入失败: %s", e)
        self._refresh_accounts()

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
        # 限制日志行数，避免长时间运行后 Tk 控件过大拖慢界面。
        self._trim_log()
        self._txt_log.see("end")
        self._txt_log.configure(state="disabled")

    # 日志区最多保留的行数（超出则从头部删除）。
    _LOG_MAX_LINES = 800

    def _trim_log(self):
        try:
            too_many = int(self._txt_log.index("end-1c").split(".")[0]) - self._LOG_MAX_LINES
            if too_many > 0:
                self._txt_log.delete("1.0", f"{too_many + 1}.0")
        except Exception:
            pass

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
        # 停止是异步的（shutdown 最长约 50ms + server_close）；在竞态窗口内重试绑定，
        # 快速「停止→启动」不再偶发端口占用。真端口冲突则 1 秒后照常报错。
        self._server = None
        for attempt in range(10):
            try:
                self._server = proxy.start_server(block=False)
                break
            except OSError as e:
                if attempt == 9:
                    proxy.log("启动失败: %s", e)
                    return
                time.sleep(0.1)
        self._running = True
        self._started_at = time.time()
        self._btn_proxy.configure(text="停止代理", fg_color="#f44336", hover_color="#c62828")
        self._update_engine_labels(running=True)
        self._update_info()

    def _stop_proxy(self):
        if not self._running:
            return
        if self._server:
            server = self._server

            def _shutdown():
                # server_close 必须显式调用：否则监听 socket 要等 GC 才释放，
                # Windows 上（已禁用 SO_REUSEADDR）停止后立刻重启会报端口占用。
                server.shutdown()
                server.server_close()

            threading.Thread(target=_shutdown, daemon=True).start()
            self._server = None
        # 重置模块级 _server 变量以便重启
        proxy._server = None
        self._running = False
        self._started_at = None
        self._btn_proxy.configure(text="启动代理", fg_color="#4CAF50", hover_color="#388E3C")
        self._update_engine_labels(running=False)
        self._lbl_info.configure(text=f"端口 {self.port}  |  模型 —")

    def _update_info(self):
        if not self._running:
            return
        model_count = len(proxy.model_cache.get("models", []))
        self._lbl_info.configure(text=f"端口 {self.port}  |  模型 {model_count} 个")
        self._ov_labels["accounts"].configure(text=f"{proxy.pool.active_count()} / {proxy.pool.size}")
        self._ov_labels["models"].configure(text=str(model_count))
        self._lbl_ov_detail.configure(text=self._overview_detail_text())
        self._render_models()
        # 定期刷新
        self.after(10_000, self._update_info)

    def _update_engine_labels(self, running: bool):
        """代理开关状态联动：顶部状态栏 + 概览卡片。"""
        text = "● 运行中" if running else "● 已停止"
        color = "#4CAF50" if running else "#f44336"
        self._lbl_status.configure(text=text, text_color=color)
        self._ov_labels["engine"].configure(text=text, text_color=color)
        self._ov_labels["endpoint"].configure(text=f"127.0.0.1:{self.port}" if running else "—")
        if not running:
            self._ov_labels["accounts"].configure(text="—")
            self._ov_labels["models"].configure(text="—")
            self._lbl_ov_detail.configure(text="—")

    def _uptime_text(self) -> str:
        if not self._started_at:
            return "—"
        h, rem = divmod(int(time.time() - self._started_at), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _overview_detail_text(self) -> str:
        current = "—"
        for a in proxy.pool.list():
            if a.enabled and a.apiKey:
                current = a.display_name
                break
        login = "OAuth 缓存：已登录" if proxy.cached_api_key else "OAuth 缓存：未登录（使用账号池）"
        return (f"运行时长：{self._uptime_text()}        程序版本：v{proxy.APP_VERSION}        "
                f"CLI 指纹：commandcode/{proxy.CC_VERSION}\n"
                f"当前账号：{current}        账号池：{proxy.pool.active_count()} 可用 / {proxy.pool.size} 总        {login}")

    # ---- 开机自启 ----
    def _toggle_autostart(self):
        enabled = bool(self._chk_autostart.get())
        if set_autostart(enabled):
            proxy.log("开机自启已%s", "开启（登录 Windows 后自动常驻托盘）" if enabled else "关闭")
            return
        # 写注册表失败：回滚勾选状态
        if enabled:
            self._chk_autostart.deselect()
        else:
            self._chk_autostart.select()

    # ---- 新版本检查 ----
    def _check_update_thread(self):
        try:
            req = urllib.request.Request(proxy.GITHUB_LATEST_URL,
                                         headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            tag = str(data.get("tag_name", "")).lstrip("vV")
            url = data.get("html_url") or proxy.GITHUB_LATEST_URL
            if not tag or not _version_tuple(tag) or not _version_tuple(proxy.APP_VERSION):
                return
            if _version_tuple(tag) > _version_tuple(proxy.APP_VERSION):
                self._dispatch(self._show_update_banner, tag, url)
        except Exception:
            pass  # 无网络/GitHub 不可达：静默跳过

    def _show_update_banner(self, tag: str, url: str):
        self._update_url = url
        self._lbl_update.configure(text=f"⬆ 新版本 v{tag}", text_color="#d29922")
        proxy.log("发现新版本 v%s（当前 %s）：点击状态栏提示可前往下载页", tag, proxy.APP_VERSION)

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
            self._dispatch(lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))
            return

        if not j.get("ok"):
            proxy.log("登录启动失败: %s", j)
            self._dispatch(lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))
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
                self._dispatch(self._refresh_accounts)
                break
            if st.get("status") in ("error", "cancelled"):
                proxy.log("授权失败: %s", st.get("message", "未知错误"))
                break
            time.sleep(1.5)
        else:
            proxy.log("登录超时（10 分钟）")

        self._dispatch(lambda: self._btn_login.configure(state="normal", text="OAuth 登录"))

    # ---- 系统托盘 ----
    def _minimize_to_tray(self):
        self.withdraw()
        if self._tray_icon is None:
            self._setup_tray()

    def _setup_tray(self):
        import pystray

        def on_show(icon, item):
            self._dispatch(self._restore_from_tray)

        def on_quit(icon, item):
            self._dispatch(self._quit_app)

        def on_toggle(icon, item):
            self._dispatch(self._toggle_proxy)
            # 更新图标颜色（在队列中执行，避免阻塞 pystray 线程）
            self._dispatch(self._refresh_tray_color, icon)

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", on_show, default=True),
            pystray.MenuItem("启动/停止代理", on_toggle),
            pystray.MenuItem("退出", on_quit),
        )

        color = "#4CAF50" if self._running else "#f44336"
        self._tray_icon = pystray.Icon(
            "cmdgo-provider", _make_tray_icon(color),
            "General-cmdgo-provider", menu,
        )
        self._tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _refresh_tray_color(self, icon=None):
        """刷新托盘图标颜色（在主子线程线程安全地执行）。"""
        try:
            color = "#4CAF50" if self._running else "#f44336"
            if icon is not None:
                icon.icon = _make_tray_icon(color)
            elif self._tray_icon is not None:
                self._tray_icon.icon = _make_tray_icon(color)
        except Exception:
            pass

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
    ap = argparse.ArgumentParser(description="General-cmdgo-provider 图形界面")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    ap.add_argument("--minimized", action="store_true", help="启动后直接最小化到系统托盘（开机自启用）")
    args = ap.parse_args()

    # 单实例保护：用命名互斥量避免启动第二个实例。
    # 否则会再注册一个托盘图标、且第二个实例的服务器绑不上 8787 端口。
    _acquire_single_instance()

    app = App(port=args.port, start_minimized=args.minimized)
    app.mainloop()


# 全局保持对互斥量句柄的引用（GC 不释放即锁持有）。
_INSTANCE_MUTEX = None


def _acquire_single_instance(name: str = "Global\\cmdgo-provider-gui") -> bool:
    """尝试获取单实例互斥量；若已有实例在运行则立即退出进程。

    返回 True 表示当前进程是唯一实例（可继续运行）；返回 False 表示已有实例。
    Windows 下返回 ERROR_ALREADY_EXISTS (183) 即已有实例在运行。
    """
    global _INSTANCE_MUTEX
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if handle:
            _INSTANCE_MUTEX = handle  # 保持引用，避免句柄被 GC 释放
            if ctypes.windll.kernel32.GetLastError() == 183:
                # 已有实例在运行：直接退出，避免第二个托盘图标。不弹窗，避免阻塞。
                try:
                    sys.exit(0)
                except SystemExit:
                    raise
        else:
            raise ctypes.WinError(ctypes.windll.kernel32.GetLastError())
    except Exception:
        # 非 Windows 或拿不到互斥量：不阻断运行。
        pass
    return True


if __name__ == "__main__":
    main()
