# -*- coding: utf-8 -*-
"""Zotero PDF2zh Server - exe 桌面启动器（瘦 exe 方案）。

- 桌面上传和插件提交共用任务执行流程，桌面翻译参数独立保存。
- 主页显示状态、上传快捷选项，以及可暂停、继续、取消的任务列表和历史。
- 内核、CUDA 组件、缓存和日志均保存在项目目录内。
- 工作线程通过队列向主线程汇报，不从后台线程操作 Tk。

运行：
    python launcher.py            # 源码调试
    zotero-pdf2zh-server.exe      # PyInstaller 产物（入口即本文件）
"""
from __future__ import annotations

import json
import os
import queue
from collections import deque
from utils.ui_jobs import UiJobs, task_rows
from utils.translation_ui import TranslationUi
from utils.version import VERSION as APP_VERSION
import subprocess
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

# ---- 先定根目录与沙盒（必须在 import server/utils 之前） ----
try:
    import app_paths  # exe 内为顶层模块；源码时同目录
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app_paths

HOME = app_paths.exe_dir()
app_paths.setup_sandbox(HOME)

SETTINGS_PATH = HOME / "settings.json"
DEFAULT_PORT = 8890

# ---------------- settings ----------------

def load_settings() -> dict:
    d = {"port": DEFAULT_PORT, "mirror_source": "",
         "check_update": False, "core_dir": "",
         "host": "127.0.0.1", "device": "auto", "gpu_id": 0, "gpu_memory_mb": 2048}
    try:
        if SETTINGS_PATH.exists():
            v = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                d.update(v)
    except Exception:
        pass
    return d


def save_settings(d: dict) -> None:
    temporary = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    temporary.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


SETTINGS = load_settings()

# ---------------- 集中日志（日志界面 + 落盘 exe/logs/app.log） ----------------
# 所有日志只写在 exe 所在文件夹内，满足“零外写”要求。

LOG_DIR = HOME / "logs"
LOG_FILE = LOG_DIR / "app.log"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

LOG_SOURCES = ("服务", "内核", "系统")


from utils.app_logging import AppLogger, LEVELS, format_entry, install_desktop_logging, log_source


APPLOG = AppLogger(LOG_FILE)


def log_system(msg: str, level=None) -> None:
    try:
        APPLOG.log("系统", msg, level)
    except Exception:
        pass


log_system(f"启动目录: {HOME}")


def effective_home() -> Path:
    return HOME


def api_base() -> str:
    return f"http://127.0.0.1:{int(SETTINGS.get('port', DEFAULT_PORT))}"


def http_get_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------- server 控制器（进程内线程） ----------------

class ServerController:
    def __init__(self, log_fn=None):
        self._srv = None
        self._thread = None
        self.running = False
        from utils.task_manager import task_manager
        self._task_manager = task_manager
        self.translator = None
        self._log = log_fn or (lambda s: None)

    def start(self) -> tuple[bool, str]:
        if self.running:
            return True, "已在运行"
        port = int(SETTINGS.get("port", DEFAULT_PORT))
        host = str(SETTINGS.get("host", "127.0.0.1"))
        try:
            import socket as _sock
            with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return False, f"端口 {port} 已被占用，请换端口或关闭占用程序。"
        except Exception:
            pass
        try:
            # 延迟导入：沙盒/补丁先生效
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from server import PDFTranslator  # type: ignore
        except ImportError:
            try:
                from server import PDFTranslator  # type: ignore
            except Exception as exc:
                return False, f"加载 server 失败: {exc}"
        except Exception as exc:
            return False, f"加载 server 失败: {exc}"
        mirror = str(SETTINGS.get("mirror_source") or "").strip()
        args = types.SimpleNamespace(
            enable_venv=True, env_tool="uv",
            enable_mirror=bool(mirror) if mirror else True,
            mirror_source=mirror or "https://mirrors.ustc.edu.cn/pypi/simple",
            skip_install=False, check_update=False, update_source="gitee",
            debug=False, enable_winexe=False, winexe_path="",
            winexe_attach_console=True,
            host=host, port=port,
        )
        try:
            import server as _server_mod  # type: ignore
            # server.py 的翻译/配置方法引用模块全局 args（源码靠 __main__ 赋值）；
            # 进程内启动必须补上，否则首次翻译即 NameError: name 'args' is not defined。
            _server_mod.args = args
        except Exception as exc:
            return False, f"初始化参数失败: {exc}"
        try:
            seeded = app_paths.seed_config_examples(HOME)
            if seeded:
                log_system("已生成配置模板: " + ", ".join(seeded))
            from server import prepare_path as _prepare_path
            with log_source("服务"):
                _prepare_path()
        except Exception as exc:
            return False, f"初始化配置失败: {exc}"
        try:
            from utils.task_manager import task_manager
            translator = PDFTranslator(args)
            self.translator = translator
            task_manager.configure_history(HOME / "logs" / "task-history.json")
            self._task_manager = task_manager
        except Exception as exc:
            return False, f"初始化失败: {exc}"
        try:
            from werkzeug.serving import make_server
            # 静默进度查询日志（复用 server._QuietAccessHandler 思想）
            try:
                from server import _QuietAccessHandler  # type: ignore
                handler = _QuietAccessHandler
            except Exception:
                handler = None
            kwargs = {"threaded": True}
            if handler is not None:
                kwargs["request_handler"] = handler
            self._srv = make_server(host, port, translator.app, **kwargs)
        except Exception as exc:
            return False, f"启动监听失败: {exc}"

        def _serve():
            try:
                self._srv.serve_forever()
            except Exception as exc:
                self._log(f"[server] 退出: {exc}")

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()
        self.running = True
        self._log(f"[server] 已启动 http://{host}:{port}")
        return True, f"已启动 http://{host}:{port}"

    def stop(self) -> None:
        server = self._srv
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
        finally:
            self._srv = None
            self.running = False
        self._log("[server] 已停止")

    def task_snapshot(self, after_revision=-1):
        if self._task_manager is None:
            return None
        return self._task_manager.desktop_snapshot(after_revision)


# ---------------- 内核工作线程 ----------------

def query_kernel_status() -> dict:
    try:
        return app_paths.kernel_status("pdf2zh_next", HOME)
    except Exception as exc:
        return {"engine": "pdf2zh_next", "exists": False, "ok": False,
                "error": f"{type(exc).__name__}: {exc}", "versions": {}}


class KernelInstaller(threading.Thread):
    """后台下载/更新内核，日志通过队列回传 UI。"""

    def __init__(self, log_q: "queue.Queue[str]", done_q: "queue.Queue[tuple]"):
        super().__init__(daemon=True)
        self.log_q = log_q
        self.done_q = done_q

    def run(self):
        import logging

        def relay(item):
            if item.source == "内核":
                try:
                    self.log_q.put_nowait(format_entry(item) + "\n")
                except queue.Full:
                    pass

        ok, tool, env_dir = False, None, None
        with APPLOG.listen(relay), log_source("内核"):
            APPLOG.log("内核", "开始准备翻译内核；进度和错误会同步显示在日志页。", "INFO")
            try:
                from utils.environment_lifecycle import transactional_install_or_update
                mirror = str(SETTINGS.get("mirror_source") or "").strip() or None
                ok, tool, env_dir = transactional_install_or_update(
                    "pdf2zh_next", env_tool="uv", preferred_index=mirror,
                    network_timeout=4.0, require_deepseek_thinking=False,
                )
            except Exception:
                logging.getLogger(__name__).exception("内核安装异常")
        self.done_q.put((bool(ok), str(tool or ""), str(env_dir or "")))

# ---------------- Apple 风 UI ----------------
# 说明：只用标准库 tkinter，保证瘦 exe 体积最小。

def _apple_style(root, scale=1.0):
    try:
        from tkinter import ttk
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        BG, CARD, ACCENT, TXT, MUTED = "#f5f5f7", "#ffffff", "#0071e3", "#1d1d1f", "#6e6e73"
        root.configure(bg=BG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Side.TFrame", background="#ececf1")
        style.configure("TLabel", background=BG, foreground=TXT, font=("Segoe UI", 11))
        style.configure("Card.TLabel", background=CARD, foreground=TXT, font=("Segoe UI", 11))
        style.configure("Title.TLabel", background=BG, foreground=TXT, font=("Segoe UI", 20, "bold"))
        style.configure("H2.TLabel", background=CARD, foreground=TXT, font=("Segoe UI", 13, "bold"))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("MutedCard.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 10))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", font=("Segoe UI", 11), padding=(round(10 * scale), round(7 * scale)))
        style.configure("Nav.TButton", font=("Segoe UI", 12), padding=round(10 * scale), anchor="w")
        style.configure("Side.TLabel", background="#ececf1", foreground=TXT, font=("Segoe UI", 10))
        style.configure("SideTitle.TLabel", background="#ececf1", foreground=TXT, font=("Segoe UI", 11, "bold"))
        style.configure("Side.TButton", font=("Segoe UI", 10), padding=(round(6 * scale), round(5 * scale)))
        style.configure("Treeview", font=("Segoe UI", 11), rowheight=round(30 * scale))
        style.configure("Treeview.Heading", font=("Segoe UI", 11, "bold"))
        style.configure("TCheckbutton", font=("Segoe UI", 11), background=CARD)
        style.configure("TRadiobutton", font=("Segoe UI", 11), background=CARD)
        style.configure("TCombobox", font=("Segoe UI", 11), padding=3)
        style.configure("TEntry", font=("Segoe UI", 11), padding=3)
        style.configure("TNotebook.Tab", font=("Segoe UI", 11), padding=(10, 5))
        from utils.checkmarks import install_checkmarks
        install_checkmarks(root, scale)
        return {"BG": BG, "CARD": CARD, "ACCENT": ACCENT, "TXT": TXT, "MUTED": MUTED}
    except Exception:
        return {"BG": "#f5f5f7", "CARD": "#ffffff", "ACCENT": "#0071e3",
                "TXT": "#1d1d1f", "MUTED": "#6e6e73"}


class LauncherApp(TranslationUi):
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root = root
        from utils.display import configure_window
        self.display = configure_window(root)
        self.scale = self.display["scale"]
        self.px = lambda value: max(1, round(value * self.scale))
        self.colors = _apple_style(root, self.scale)
        self.root.title('PDF2zh')
        self.window_icons = [tk.PhotoImage(master=root, file=str(app_paths.branding_path(f'logo-{size}.png')))
                             for size in (16,32,48,64,96,128,256)]
        self.root.iconbitmap(default=str(app_paths.branding_path('app.ico')))
        self.root.iconphoto(True, *self.window_icons)
        if os.name == 'nt':
            __import__('ctypes').windll.shell32.SetCurrentProcessExplicitAppUserModelID('erxinzi.PDF2zh')
        logo_size = min((size for size in (32,48,64,96,128,256) if size >= self.px(64)), default=256)
        self.brand_logo = self.window_icons[(16,32,48,64,96,128,256).index(logo_size)]
        self.root.report_callback_exception = lambda *exc: __import__("logging").getLogger(__name__).error("界面回调异常", exc_info=exc)
        self.jobs = UiJobs()
        self._server_busy = False
        self._closing = False
        self._poll_timer = None
        self._tasks_revision = -1
        self._next_task_refresh = 0.0
        self._download_lines = deque(maxlen=300)
        self.ctl = ServerController(log_fn=self._on_server_log)
        self.kernel_info = {"exists": False, "ok": False, "versions": {}}
        self.log_q: queue.Queue[str] = queue.Queue(maxsize=500)
        self.done_q: queue.Queue[tuple] = queue.Queue()
        self.installer = None
        self.init_translation(SETTINGS, save_settings)
        self._build()
        self.seed_translation_profiles(app_paths.config_dir(HOME), SETTINGS)
        history_name = "self-test-history.json" if "--self-test" in sys.argv else "task-history.json"
        manager = self.ctl._task_manager
        self.jobs.submit("history-load", lambda: manager.configure_history(HOME / "logs" / history_name),
                         lambda _: self.refresh_tasks())
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._poll_queues()
        self.refresh_kernel_status(silent=True)
        self.refresh_tasks()

    # ---------- 布局 ----------
    def _build(self):
        tk, ttk = self.tk, __import__("tkinter.ttk", fromlist=["ttk"])
        C = self.colors
        outer = ttk.Frame(self.root, style="TFrame")
        outer.pack(fill="both", expand=True)
        # 侧边栏
        side = ttk.Frame(outer, style="Side.TFrame", width=self.px(210))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.brand_header = ttk.Frame(side, style="Side.TFrame")
        self.brand_header.pack(fill="x", pady=(self.px(16), self.px(14)), padx=self.px(10))
        ttk.Label(self.brand_header, image=self.brand_logo, background="#ececf1").pack(side="left")
        ttk.Label(self.brand_header, text="PDF2zh", font=("Segoe UI", 16, "bold"),
                  background="#ececf1").pack(side="left", padx=(self.px(8), 0))
        self.nav_btns = {}

        def _nav(key: str, label: str):
            b = ttk.Button(side, text=label, style="Nav.TButton",
                           command=lambda k=key: self.show_page(k))
            b.pack(fill="x", padx=10, pady=3)
            self.nav_btns[key] = b
            return b

        def _section(text: str):
            ttk.Label(side, text=text, background="#ececf1",
                      foreground=C["MUTED"], font=("Segoe UI", 10)).pack(
                          padx=12, pady=(10, 2), anchor="w")

        _nav("home", "  开始")
        _section("设置")
        _nav("translation", "  翻译")
        _nav("kernel", "  内核")
        _nav("general", "  通用")
        _section("诊断")
        _nav("logs", "  日志")
        from utils.links import LinkLabel, UPSTREAM_URL, PROJECT_URL
        footer = ttk.Frame(side, style="Side.TFrame")
        footer.pack(side="bottom", fill="x", padx=12, pady=14)
        self.footer_version = ttk.Label(footer, text=f"v{APP_VERSION}", background="#ececf1",
                                        foreground=C["MUTED"], font=("Segoe UI", 9))
        self.footer_version.pack(anchor="w", pady=(0, 4))
        credits = ttk.Frame(footer, style="Side.TFrame"); credits.pack(anchor="w")
        style = {"background": "#ececf1", "foreground": "#0071e3", "font": ("Segoe UI", 10)}
        self.upstream_link = LinkLabel(credits, "PDF2zh", UPSTREAM_URL,
            "本项目基于zotero pdf2zh二次开发，点击跳转原作者GitHub", **style)
        self.upstream_link.pack(side="left")
        ttk.Label(credits, text=" by ", background="#ececf1", font=("Segoe UI", 10)).pack(side="left")
        self.project_link = LinkLabel(credits, "erxinzi", PROJECT_URL, "点击跳转本项目主页", **style)
        self.project_link.pack(side="left")
        self._build_sidebar_status(side)
        # 内容区
        self.content = ttk.Frame(outer, style="TFrame")
        self.content.pack(side="left", fill="both", expand=True, padx=self.px(24), pady=self.px(18))
        self.pages = {}
        self.page_bodies = {}
        self.page_canvases = {}
        for key in ("home", "translation", "kernel", "general", "logs"):
            page = ttk.Frame(self.content, style="TFrame")
            self.pages[key] = page
            if key == "logs":
                self.page_bodies[key] = page
                continue
            canvas = tk.Canvas(page, bg=C["BG"], highlightthickness=0, yscrollincrement=self.px(28))
            scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            body = ttk.Frame(canvas, style="TFrame")
            window = canvas.create_window((0, 0), window=body, anchor="nw")
            body.bind("<Configure>", lambda e, target=canvas: target.configure(scrollregion=target.bbox("all")))
            canvas.bind("<Configure>", lambda e, target=canvas, item=window: target.itemconfigure(item, width=e.width))
            self.page_bodies[key] = body
            self.page_canvases[key] = canvas
        self.kernel_canvas = self.page_canvases["kernel"]
        self._build_home()
        self._build_translation()
        self._build_kernel()
        self._build_general()
        self._build_logs()
        self.show_page("home")
        from utils.wheel_focus import WheelFocus
        self.wheel_focus = WheelFocus(self.root, self._scroll_current_page)

    def _card(self, parent, title: str, *, pack=True):
        from tkinter import ttk
        card = ttk.Frame(parent, style="Card.TFrame", padding=self.px(18))
        if pack:
            card.pack(fill="x", pady=self.px(8))
        ttk.Label(card, text=title, style="H2.TLabel").pack(anchor="w", pady=(0, 8))
        return card

    def _build_sidebar_status(self, side):
        from tkinter import ttk
        from utils.links import Tooltip
        self.sidebar_status = ttk.Frame(side, style="Side.TFrame")
        self.sidebar_status.pack(side="bottom", fill="x", padx=self.px(12), pady=self.px(12))
        self.sidebar_status.columnconfigure(2, weight=1)
        ttk.Label(self.sidebar_status, text="服务状态", style="SideTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, self.px(8)))
        self.kernel_dot = ttk.Label(self.sidebar_status, text="●", style="Side.TLabel", foreground="#c80")
        self.kernel_dot.grid(row=1, column=0, sticky="w")
        self.kernel_text = ttk.Label(self.sidebar_status, text="Core", style="Side.TLabel")
        self.kernel_text.grid(row=1, column=1, sticky="w", padx=self.px(5))
        self.kernel_settings_button = ttk.Button(self.sidebar_status, text="内核设置", style="Side.TButton",
                                                command=lambda: self.show_page("kernel"))
        self.kernel_settings_button.grid(row=1, column=2, sticky="e", pady=self.px(3))
        self.kernel_tooltip = Tooltip(self.kernel_text, "内核正在检查…")
        self.server_dot = ttk.Label(self.sidebar_status, text="●", style="Side.TLabel", foreground="#c00")
        self.server_dot.grid(row=2, column=0, sticky="w")
        self.server_text = ttk.Label(self.sidebar_status, text="服务状态", style="Side.TLabel")
        self.server_text.grid(row=2, column=1, sticky="w", padx=self.px(5))
        self.server_btn = ttk.Button(self.sidebar_status, text="启动服务", style="Side.TButton", command=self.toggle_server)
        self.server_btn.grid(row=2, column=2, sticky="e", pady=self.px(3))
        self.server_tooltip = Tooltip(self.server_text, "服务未启动\n" + api_base())

    # ---------- 开始页 ----------
    def _build_home(self):
        from tkinter import ttk
        f = self.page_bodies["home"]
        ttk.Label(f, text="开始", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="选择本地 PDF 开始翻译，或启动服务后从 Zotero 插件提交。",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 6))
        self._build_upload(f)
        self._build_task_tables(f)

    # ---------- 内核设置页 ----------
    def _build_kernel(self):
        from tkinter import ttk
        import tkinter as tk
        f = self.page_bodies["kernel"]
        ttk.Label(f, text="设置 · 内核", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="翻译内核（pdf2zh-next）在首次使用时联网下载，固定存放在 exe 目录下的 core/ 内。",
                  style="Muted.TLabel", wraplength=self.px(700), justify="left").pack(anchor="w", pady=(2, 6))
        c1 = self._card(f, "内核保存位置")
        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x")
        self.core_var = tk.StringVar(value=str(app_paths.core_dir(HOME)))
        e = ttk.Entry(row, textvariable=self.core_var, width=70, state="readonly")
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="打开目录",
                   command=lambda: self._open_dir(app_paths.core_dir(HOME))).pack(side="right", padx=(6, 0))
        ttk.Label(c1, text="默认 <exe目录>/core，不支持改到 exe 目录之外（按打包要求锁定）。",
                  style="MutedCard.TLabel").pack(anchor="w", pady=(6, 0))
        c2 = self._card(f, "内核信息")
        self.kernel_info_text = tk.Text(c2, height=5, wrap="word", relief="flat",
                                        font=("Consolas", 10), bg="#f5f5f7")
        self.kernel_info_text.pack(fill="x")
        self.kernel_info_text.configure(state="disabled")
        row2 = ttk.Frame(c2, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="刷新内核信息",
                   command=lambda: self.refresh_kernel_status()).pack(side="left")
        self.dl_btn = ttk.Button(row2, text="下载 / 更新内核", style="Accent.TButton",
                                 command=self.start_install_kernel)
        self.dl_btn.pack(side="left", padx=6)
        gpu = self._card(f, "CUDA 加速")
        ttk.Label(gpu, text="GPU 用于文档版面识别；CUDA 不可用时自动使用 CPU。",
                  style="MutedCard.TLabel").pack(anchor="w")
        ttk.Label(gpu, text=f"组件位置：{HOME / 'core/onnxruntime-cuda'}", style="MutedCard.TLabel",
                  wraplength=self.px(760), justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(gpu, text="CUDA 下载、临时文件和 GPU 运行缓存均限制在 core 内；仅供本项目使用。",
                  style="MutedCard.TLabel", wraplength=self.px(760), justify="left").pack(anchor="w", pady=(4, 0))
        options = ttk.Frame(gpu, style="Card.TFrame")
        options.pack(fill="x", pady=8)
        self.gpu_modes = {"自动（CUDA 优先）": "auto", "仅 CPU": "cpu", "CUDA（失败回退 CPU）": "cuda"}
        selected = next((label for label, value in self.gpu_modes.items() if value == SETTINGS.get("device", "auto")), "自动（CUDA 优先）")
        self.gpu_mode_var = tk.StringVar(value=selected)
        ttk.Label(options, text="计算模式", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.gpu_mode_var, values=list(self.gpu_modes), state="readonly", width=25).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Label(options, text="GPU 编号", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        self.gpu_id_var = tk.StringVar(value=str(SETTINGS.get("gpu_id", 0)))
        ttk.Spinbox(options, from_=0, to=15, textvariable=self.gpu_id_var, width=8).grid(row=1, column=1, padx=8, sticky="w")
        ttk.Label(options, text="显存上限（MB）", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        self.gpu_memory_var = tk.StringVar(value=str(SETTINGS.get("gpu_memory_mb", 2048)))
        ttk.Entry(options, textvariable=self.gpu_memory_var, width=10).grid(row=2, column=1, padx=8, sticky="w")
        actions = ttk.Frame(gpu, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 6))
        ttk.Button(actions, text="保存加速设置", command=self.save_gpu_settings).pack(side="left")
        self.cuda_install_btn = ttk.Button(actions, text="安装 / 更新 CUDA 组件", command=lambda: self.run_gpu_task(install=True))
        self.cuda_install_btn.pack(side="left", padx=6)
        self.cuda_probe_btn = ttk.Button(actions, text="检测 CUDA", command=self.run_gpu_task)
        self.cuda_probe_btn.pack(side="left")
        cuda_installed = (HOME / "core/onnxruntime-cuda/ready.json").exists()
        self.gpu_status_var = tk.StringVar(value="CUDA 组件已安装，可点击检测 CUDA 验证当前显卡。" if cuda_installed else "尚未检测。安装组件会下载 GPU 运行库，保存在 core/ 中。")
        ttk.Label(gpu, textvariable=self.gpu_status_var, style="MutedCard.TLabel", wraplength=self.px(660), justify="left").pack(anchor="w")
        c3 = self._card(f, "内核日志")
        self.dl_log = tk.Text(c3, height=6, wrap="word", relief="flat",
                              font=("Consolas", 10), bg="#f5f5f7")
        self.dl_log.pack(fill="both", expand=True)

    # ---------- 通用设置页 ----------
    def _build_general(self):
        from tkinter import ttk
        import tkinter as tk
        f = self.page_bodies["general"]
        ttk.Label(f, text="设置 · 通用", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="端口等设置保存在 exe 目录下的 settings.json，缓存固定在 exe 目录下的 cache/。",
                  style="Muted.TLabel", wraplength=self.px(700), justify="left").pack(anchor="w", pady=(2, 6))
        c1 = self._card(f, "服务")
        grid = ttk.Frame(c1, style="Card.TFrame")
        grid.pack(fill="x")
        ttk.Label(grid, text="端口", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=4)
        self.port_var = tk.StringVar(value=str(SETTINGS.get("port", DEFAULT_PORT)))
        ttk.Entry(grid, textvariable=self.port_var, width=12).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(grid, text="Zotero 插件里的 Server 地址端口须与此一致。",
                  style="MutedCard.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(grid, text="PyPI 镜像", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.mirror_var = tk.StringVar(value=str(SETTINGS.get("mirror_source") or ""))
        ttk.Entry(grid, textvariable=self.mirror_var, width=52).grid(
            row=1, column=1, columnspan=2, sticky="w", padx=8)
        ttk.Label(grid, text="留空=自动选择；大陆用户建议保留 USTC 镜像。",
                  style="MutedCard.TLabel").grid(row=2, column=1, columnspan=2, sticky="w")
        self.check_update_var = tk.BooleanVar(value=bool(SETTINGS.get("check_update", False)))
        ttk.Checkbutton(grid, text="启动时检查 Server 更新（默认关闭，避免 exe 自改写）",
                        variable=self.check_update_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Button(c1, text="保存设置（需重启服务生效）",
                   style="Accent.TButton", command=self.save_general).pack(anchor="w", pady=(8, 0))
        c2 = self._card(f, "目录")
        ttk.Label(c2, text=f"配置目录: {app_paths.config_dir(HOME)}",
                  style="Card.TLabel", wraplength=self.px(680), justify="left").pack(anchor="w")
        ttk.Label(c2, text=f"输出目录: {app_paths.output_dir(HOME)}",
                  style="Card.TLabel", wraplength=self.px(680), justify="left").pack(anchor="w")
        ttk.Label(c2, text=f"缓存目录: {app_paths.cache_dir(HOME)}（uv / pip / 模型缓存）",
                  style="Card.TLabel", wraplength=self.px(680), justify="left").pack(anchor="w")
        row = ttk.Frame(c2, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="打开输出目录",
                   command=lambda: self._open_dir(app_paths.output_dir(HOME))).pack(side="left")
        ttk.Button(row, text="打开缓存目录",
                   command=lambda: self._open_dir(app_paths.cache_dir(HOME))).pack(side="left", padx=6)
        ttk.Button(row, text="清理缓存（保留内核）",
                   command=self.clean_cache).pack(side="left")
        self.cache_size_label = ttk.Label(row, text="", style="MutedCard.TLabel")
        self.cache_size_label.pack(side="right")
        self.refresh_cache_size()

    # ---------- 日志页（第三组界面：服务 / 内核 / 系统） ----------
    def _build_logs(self):
        from tkinter import ttk
        import tkinter as tk
        f = self.page_bodies["logs"]
        ttk.Label(f, text="日志", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="服务 / 内核 / 系统三类运行日志。日志文件只保存在 exe 目录下的 logs/ 内。",
                  style="Muted.TLabel", wraplength=self.px(700), justify="left").pack(anchor="w", pady=(2, 6))
        c = self._card(f, "运行日志")
        c.pack_configure(fill="both", expand=True)
        bar = ttk.Frame(c, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="来源", style="Card.TLabel").pack(side="left")
        self.log_source_var = tk.StringVar(value="全部")
        src = ttk.Combobox(bar, textvariable=self.log_source_var, width=10, state="readonly",
                           values=["全部", "服务", "内核", "系统"])
        src.pack(side="left", padx=6)
        src.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs_view())
        ttk.Label(bar, text="级别", style="Card.TLabel").pack(side="left", padx=(8, 0))
        self.log_level_var = tk.StringVar(value="全部")
        levels = ttk.Combobox(bar, textvariable=self.log_level_var, width=9, state="readonly",
                              values=["全部", *LEVELS])
        levels.pack(side="left", padx=6)
        levels.bind("<<ComboboxSelected>>", lambda e: self.refresh_logs_view())
        ttk.Label(bar, text="搜索", style="Card.TLabel").pack(side="left", padx=(8, 0))
        self.log_search_var = tk.StringVar(value="")
        se = ttk.Entry(bar, textvariable=self.log_search_var, width=16)
        se.pack(side="left", padx=6)
        se.bind("<KeyRelease>", lambda e: self.refresh_logs_view())
        self.log_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动滚动", variable=self.log_autoscroll_var).pack(side="left", padx=6)
        txt_frame = ttk.Frame(c, style="Card.TFrame")
        txt_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(txt_frame, wrap="word", relief="flat",
                                font=("Consolas", 10), bg="#f5f5f7", height=18)
        sb = ttk.Scrollbar(txt_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_text.configure(state="disabled")
        try:
            self.log_text.tag_configure("level-INFO", foreground="#2866a3")
            self.log_text.tag_configure("level-WARNING", foreground="#9b6500")
            self.log_text.tag_configure("level-ERROR", foreground="#c62828")
            self.log_text.tag_configure("time", foreground="#6e6e73")
            self.log_text.tag_configure("src-服务", foreground="#0071e3")
            self.log_text.tag_configure("src-内核", foreground="#7a4fd0")
            self.log_text.tag_configure("src-系统", foreground="#3a3a3c")
        except Exception:
            pass
        brow = ttk.Frame(c, style="Card.TFrame")
        brow.pack(fill="x", pady=(8, 0))
        ttk.Button(brow, text="刷新", command=self.refresh_logs_view).pack(side="left")
        ttk.Button(brow, text="清空显示", command=self.clear_logs_view).pack(side="left", padx=6)
        ttk.Button(brow, text="保存日志...", command=self.save_logs_file).pack(side="left")
        ttk.Button(brow, text="打开日志目录",
                   command=lambda: self._open_dir(LOG_DIR)).pack(side="left", padx=6)
        self.log_status_label = ttk.Label(brow, text="", style="MutedCard.TLabel")
        self.log_status_label.pack(side="right")
        self.refresh_logs_view()

    def _log_match(self, ts, src, level, msg) -> bool:
        try:
            f = self.log_source_var.get()
        except Exception:
            f = "全部"
        if f and f != "全部" and src != f:
            return False
        selected = self.log_level_var.get()
        if selected != "全部" and selected != level:
            return False
        try:
            kw = self.log_search_var.get().strip().lower()
        except Exception:
            kw = ""
        if kw and kw not in (msg or "").lower():
            return False
        return True

    def _write_log_lines(self, ts, src, level, msg):
        for line in str(msg).splitlines() or [""]:
            self.log_text.insert("end", f"[{ts}] ", "time")
            self.log_text.insert("end", f"[{src}] ", f"src-{src}")
            self.log_text.insert("end", f"[{level}] ", f"level-{level}")
            self.log_text.insert("end", f"{line}\n", f"level-{level}")

    def _trim_log_text(self, max_lines: int = 3000):
        try:
            count = int(self.log_text.index("end-1c").split(".")[0])
            if count > max_lines:
                self.log_text.delete("1.0", f"{count - max_lines}.0")
        except Exception:
            pass

    def _insert_log_batch(self, entries, replace=False):
        visible = [entry for entry in entries if self._log_match(*entry)]
        if not visible and not replace:
            return
        self.log_text.configure(state="normal")
        if replace:
            self.log_text.delete("1.0", "end")
        segments = []
        for entry in visible:
            text, tag = format_entry(entry) + "\n", "level-" + entry.level
            if segments and segments[-1] == tag:
                segments[-2] += text
            else:
                segments.extend((text, tag))
        if segments:
            self.log_text.insert("end", *segments)
        self._trim_log_text()
        self.log_text.configure(state="disabled")
        if self._autoscroll_on():
            self.log_text.see("end")
        self._paint_log_status()

    def refresh_logs_view(self):
        entries = [entry for entry in APPLOG.snapshot(clear_pending=True) if self._log_match(*entry)][-1000:]
        self._insert_log_batch(entries, replace=True)

    def _autoscroll_on(self) -> bool:
        try:
            return bool(self.log_autoscroll_var.get())
        except Exception:
            return True

    def _append_log_entry(self, ts, src, level, msg):
        from utils.app_logging import LogEntry
        self._insert_log_batch([LogEntry(ts, src, level, msg)])

    def _paint_log_status(self):
        try:
            total = len(APPLOG.entries)
            size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
            self.log_status_label.configure(
                text=f"{total} 条 · app.log {size / 1024:.0f} KB")
        except Exception:
            pass

    def clear_logs_view(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def save_logs_file(self):
        try:
            from tkinter import filedialog
            dst = filedialog.asksaveasfilename(
                defaultextension=".log",
                filetypes=[("Log", "*.log"), ("All", "*.*")],
                initialfile="pdf2zh-server-" + time.strftime("%Y%m%d-%H%M%S") + ".log")
            if not dst:
                return
            with open(dst, "w", encoding="utf-8", errors="replace") as fp:
                for ts, src, level, msg in APPLOG.snapshot():
                    if not self._log_match(ts, src, level, msg):
                        continue
                    for line in str(msg).splitlines() or [""]:
                        fp.write(f"[{ts}] [{src}] [{level}] {line}\n")
            self._msg("已保存", "当前过滤条件下的日志已保存到:\n" + str(dst))
            log_system("日志已导出: " + str(dst))
        except Exception as exc:
            self._msg("保存失败", str(exc), error=True)

    def _scroll_current_page(self, event):
        canvas = self.page_canvases.get(getattr(self, "current_page", ""))
        if canvas is None:
            return
        if str(event.widget).startswith(str(canvas)):
            from utils.wheel_focus import WheelFocus
            canvas.yview_scroll(WheelFocus.units(event), "units")
            return "break"

    # ---------- 页面切换 ----------
    def show_page(self, key: str):
        self.current_page = key
        if hasattr(self, 'wheel_focus'):
            self.wheel_focus.reset()
        for k, f in self.pages.items():
            f.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        if key == "logs":
            self.refresh_logs_view()
        elif key == "kernel" and self._download_lines:
            self._set_text(self.dl_log, "".join(self._download_lines))

    # ---------- 服务 ----------
    def toggle_server(self):
        if self._server_busy:
            return
        stopping = self.ctl.running
        self._server_busy = True
        self.server_btn.configure(state="disabled", text="停止中…" if stopping else "启动中…")
        self.server_tooltip.text = "正在关闭服务…" if stopping else "正在初始化服务…"

        def work():
            if stopping:
                self.ctl.stop()
                return True, "服务已停止"
            return self.ctl.start()

        def done(result):
            self._server_busy = False
            ok, message = result
            log_system(message, "INFO" if ok else "ERROR")
            self._paint_server()
            self._tasks_revision = -1
            self.refresh_tasks()

        def failed(error):
            self._server_busy = False
            log_system("服务操作失败：" + str(error), "ERROR")
            self._paint_server()

        self.jobs.submit("server-control", work, done, failed)

    def _paint_server(self):
        self.server_btn.configure(state="normal")
        if self.ctl.running:
            self.server_dot.configure(foreground="#090")
            self.server_btn.configure(text="关闭服务")
        else:
            self.server_dot.configure(foreground="#c00")
            self.server_btn.configure(text="启动服务")
        self.server_tooltip.text = ("服务运行中" if self.ctl.running else "服务未启动") + "\n" + api_base()

    def _on_server_log(self, s: str):
        try:
            APPLOG.log("服务", s)
        except Exception:
            pass

    # ---------- 任务 ----------
    def refresh_tasks(self):
        snapshot = self.ctl.task_snapshot(self._tasks_revision)
        if snapshot is not None:
            self._tasks_revision, tasks, history = snapshot
            self._paint_tasks(tasks, history)

    def _paint_tasks(self, tasks, history):
        self._all_tasks, self._all_history = tasks, history
        self._task_records = {str(task.get("taskId")): task for task in history + tasks}
        active = [task for task in tasks if task.get("active") is True]
        finished = list(history)
        seen = {task.get("taskId") for task in finished}
        finished.extend(task for task in tasks if task.get("active") is False and task.get("taskId") not in seen)
        for view, data in (("active", active), ("history", finished)):
            tree = self.task_trees[view]
            rows = task_rows(data, [])
            previous = getattr(self, "_rows_" + view, {})
            current = dict(rows)
            for key in previous.keys() - current.keys():
                tree.delete(key)
            for key, values in rows:
                if key not in previous:
                    tree.insert("", "end", iid=key, values=values)
                elif previous[key] != values:
                    tree.item(key, values=values)
            order = [key for key, _ in rows]
            if list(tree.get_children()) != order:
                for index, key in enumerate(order):
                    tree.move(key, "", index)
            setattr(self, "_rows_" + view, current)
        self._task_row_values = {**getattr(self, "_rows_history", {}), **getattr(self, "_rows_active", {})}
        self._task_details()

    # ---------- 内核 ----------
    def refresh_kernel_status(self, silent=False):
        self.jobs.submit("kernel-status", query_kernel_status, self._paint_kernel)

    def _paint_kernel(self, info: dict):
        self.kernel_info = info
        ok, exists = bool(info.get("ok")), bool(info.get("exists"))
        vers = info.get("versions") or {}
        if ok:
            txt = "就绪  " + ", ".join(f"{k}={v}" for k, v in vers.items() if v)
            col = "#090"
        elif exists:
            txt = "已创建但不完整，建议点击“下载 / 更新内核”修复"
            col = "#c80"
        else:
            txt = "未安装，点击“前往设置 · 内核”下载（需联网）"
            col = "#c00"
        try:
            self.kernel_dot.configure(foreground=col)
            self.kernel_tooltip.text = txt
        except Exception:
            pass
        lines = [f"引擎: pdf2zh_next",
                 f"venv: {info.get('venv_dir', app_paths.kernel_venv_dir(home=HOME))}",
                 f"python: {info.get('python', app_paths.kernel_python(home=HOME))}",
                 f"存在: {'是' if exists else '否'}   可用: {'是' if ok else '否'}"]
        if vers:
            lines.append("版本: " + ", ".join(f"{k}={v}" for k, v in vers.items()))
        if info.get("error"):
            lines.append("备注: " + str(info["error"]))
        self._set_text(self.kernel_info_text, "\n".join(lines))

    def save_gpu_settings(self):
        try:
            values = {"device": self.gpu_modes[self.gpu_mode_var.get()],
                      "gpu_id": int(self.gpu_id_var.get()), "gpu_memory_mb": int(self.gpu_memory_var.get())}
            if values["gpu_id"] < 0 or not 256 <= values["gpu_memory_mb"] <= 65536:
                raise ValueError("GPU 编号不能为负数，显存上限应为 256–65536 MB")
            SETTINGS.update(values)
            save_settings(SETTINGS)
            self.gpu_status_var.set("加速设置已保存，从下一个翻译任务开始生效。")
            log_system(f"加速设置已保存：模式 {values['device']}，GPU {values['gpu_id']}，显存上限 {values['gpu_memory_mb']} MB", "INFO")
            return True
        except Exception as exc:
            self.gpu_status_var.set(str(exc))
            log_system("加速设置无效：" + str(exc), "ERROR")
            return False

    def _kernel_change_allowed(self):
        from utils.task_manager import task_manager
        if any(task.get("active") for task in task_manager.get_active_tasks_list()):
            log_system("翻译正在进行，请待任务结束后安装或更新内核组件。", "WARNING")
            return False
        if getattr(self, "_gpu_busy", False) or (self.installer and self.installer.is_alive()):
            log_system("内核组件正在处理，请等待完成。", "WARNING")
            return False
        return True

    def run_gpu_task(self, install=False):
        if not self._kernel_change_allowed() or not self.save_gpu_settings():
            return
        python = app_paths.kernel_python(home=HOME)
        if not python.exists():
            self.gpu_status_var.set("请先下载翻译内核，再安装或检测 CUDA。")
            log_system("CUDA 检测需要先安装翻译内核", "WARNING")
            return
        self._gpu_busy = True
        self.cuda_install_btn.configure(state="disabled")
        self.cuda_probe_btn.configure(state="disabled")
        self.gpu_status_var.set("正在安装 CUDA 组件…" if install else "正在检测显卡及 CUDA 实际运算…")

        def work():
            import logging
            from utils.gpu_runtime import detect_nvidia, install_cuda, probe_cuda
            message = ""
            with log_source("内核"):
                try:
                    APPLOG.log("内核", "NVIDIA 显卡 / 驱动：" + detect_nvidia(), "INFO")
                    if install:
                        install_cuda(HOME, python, str(SETTINGS.get("mirror_source") or "").strip() or None)
                    else:
                        probe_cuda(HOME, python)
                    message = "CUDA 检测通过，已验证 GPU 实际运算。下一次翻译将使用保存的计算模式。"
                    APPLOG.log("内核", message, "INFO")
                except Exception:
                    logging.getLogger(__name__).exception("CUDA 组件操作失败；CPU 翻译仍可使用")
                    message = "CUDA 尚不可用，请在日志页查看 ERROR / WARNING；CPU 翻译仍可使用。"

            return message

        def apply(message):
            self._gpu_busy = False
            self.cuda_install_btn.configure(state="normal")
            self.cuda_probe_btn.configure(state="normal")
            self.gpu_status_var.set(message)

        self.jobs.submit("cuda", work, apply)

    def start_install_kernel(self):
        if not self._kernel_change_allowed():
            return
        if self.installer and self.installer.is_alive():
            return
        try:
            self.dl_btn.configure(state="disabled", text="下载中…")
        except Exception:
            pass
        self._set_text(self.dl_log, "")
        self.installer = KernelInstaller(self.log_q, self.done_q)
        self.installer.start()
        log_system("开始下载/更新内核")

    # ---------- 通用 ----------
    def save_general(self):
        try:
            port = int(str(self.port_var.get()).strip())
            assert 1 <= port <= 65535
        except Exception:
            self._msg("保存失败", "端口必须是 1-65535 的数字。", error=True)
            return
        SETTINGS["port"] = port
        SETTINGS["mirror_source"] = str(self.mirror_var.get()).strip()
        SETTINGS["check_update"] = bool(self.check_update_var.get())
        save_settings(SETTINGS)
        self._paint_server()
        self._msg("已保存", f"设置已写入 {SETTINGS_PATH}\n重启服务后生效。")
        log_system("设置已保存: 端口=" + str(SETTINGS.get("port")))

    def refresh_cache_size(self):
        def work():
            total, count = 0, 0
            for path in app_paths.cache_dir(HOME).rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                        count += 1
                except OSError:
                    pass
            return total, count

        def apply(result):
            total, count = result
            self.cache_size_label.configure(text=f"约 {total / 1024 / 1024:.1f} MB / {count} 文件")

        self.jobs.submit("cache-size", work, apply)

    def clean_cache(self):
        def work():
            import shutil
            keep = {"uv-cache", "pip-cache"}
            cdir = app_paths.cache_dir(HOME)
            removed = 0
            for child in ("huggingface", "torch", "modelscope", "tmp", "xdg-config", "share"):
                p = cdir / child
                try:
                    if p.exists():
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    pass
            # pip/uv 缓存只清内容不删目录
            for child in keep:
                p = cdir / child
                try:
                    if p.exists():
                        for sub in p.iterdir():
                            try:
                                if sub.is_dir():
                                    shutil.rmtree(sub, ignore_errors=True)
                                else:
                                    sub.unlink(missing_ok=True)
                            except Exception:
                                pass
                        removed += 1
                except Exception:
                    pass

        def done(_):
            self.refresh_cache_size()
            self._msg("清理完成", "已清理模型/临时缓存，翻译内核未动。")
        self.jobs.submit("clean-cache", work, done)

    # ---------- 队列轮询 / 工具 ----------
    def _poll_queues(self):
        self._poll_timer = None
        self.jobs.drain()
        if self._closing:
            if self.jobs.busy("shutdown"):
                self._poll_timer = self.root.after(50, self._poll_queues)
            return
        now = time.monotonic()
        if now >= self._next_task_refresh:
            self._next_task_refresh = now + 0.25
            self.refresh_tasks()
        download = []
        for _ in range(20):
            try:
                download.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if download:
            self._download_lines.extend(download)
            if self.current_page == "kernel":
                self._append_text(self.dl_log, "".join(download))
        if self.current_page == "logs":
            entries = []
            deadline = time.perf_counter() + 0.004
            while len(entries) < 40 and time.perf_counter() < deadline:
                try:
                    entries.append(APPLOG.ui_queue.get_nowait())
                except queue.Empty:
                    break
            if entries:
                self._insert_log_batch(entries)
        for _ in range(5):
            try:
                ok, tool, env_dir = self.done_q.get_nowait()
            except queue.Empty:
                break
            self.dl_btn.configure(state="normal", text="下载 / 更新内核")
            self.refresh_kernel_status(silent=True)
            log_system(f"内核就绪: {tool} {env_dir}" if ok else "内核安装未完成，请查看 ERROR 日志", "INFO" if ok else "ERROR")
        self._poll_timer = self.root.after(50, self._poll_queues)

    def _close(self):
        if self._closing:
            return
        self._closing = True
        from utils.task_manager import task_manager
        active = [task['taskId'] for task in task_manager.get_active_tasks_list() if task.get('active')]
        def finish(_=None):
            self.jobs.close()
            if self._poll_timer:
                self.root.after_cancel(self._poll_timer)
                self._poll_timer = None
            self.root.destroy()
        if not active and (not isinstance(self.ctl, ServerController) or not self.ctl.running):
            finish()
            return
        self.root.withdraw()
        def shutdown():
            for task_id in active:
                try:
                    task_manager.act(task_id, 'cancel')
                except ValueError:
                    pass
            if isinstance(self.ctl, ServerController):
                self.ctl.stop()
        self.jobs.submit('shutdown', shutdown, finish, lambda exc: (log_system(str(exc), 'ERROR'), finish()))

    def _set_text(self, w, s: str):
        try:
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.insert("end", s)
            w.configure(state="disabled")
        except Exception:
            pass

    def _append_text(self, w, s: str):
        try:
            w.configure(state="normal")
            w.insert("end", s)
            w.see("end")
            w.configure(state="disabled")
        except Exception:
            pass

    def _open_dir(self, p: Path):
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(p)])
            else:
                subprocess.run(["xdg-open", str(p)])
        except Exception as exc:
            self._msg("打开失败", str(exc), error=True)

    def _msg(self, title: str, msg: str, error=False):
        # Runtime notices belong to the log page, never a modal dialog.
        log_system(f"{title}: {msg}", "ERROR" if error else "INFO")
        if error:
            self.show_page("logs")



def responsiveness_self_test(app, python):
    """Exercise actual Tk callbacks, a log burst and a live hidden child, offline."""
    from runtime_progress import PREFIX
    from utils.execute import _execute_with_capture
    from utils.task_manager import task_manager
    task_id = "desktop-progress-self-test"
    task_manager.add_task(task_id, {"taskId": task_id, "fileName": "self-test.pdf", "active": True, "progress": 0, "engine": "pdf2zh_next"})

    class Controller:
        running = False

        def start(self):
            time.sleep(0.35)
            self.running = True
            return True, "自检服务就绪"

        def task_snapshot(self, revision=-1):
            return task_manager.desktop_snapshot(revision)

    app.ctl = Controller()
    app._tasks_revision = -1
    app.show_page("home")
    insertions = []
    original_insert = app.log_text.insert

    def insert(*args):
        insertions.append(app.current_page)
        return original_insert(*args)

    app.log_text.insert = insert
    beginning = time.perf_counter()
    app.toggle_server()
    click_ms = (time.perf_counter() - beginning) * 1000
    assert click_ms < 150, f"Service click blocked for {click_ms:.1f} ms"

    def flood():
        for index in range(5000):
            APPLOG.log("内核", f"自检日志压力 {index}", "INFO")

    app.jobs.submit("selftest-log-burst", flood, lambda _: None)
    child = "import json,time\nfor value in (10,45,80,100):\n print(" + repr(PREFIX) + "+json.dumps({'version':1,'progress':value,'stage':'Translate Paragraphs'}), flush=True)\n time.sleep(0.4)\n"
    child_env = app_paths.child_env({"PDF2ZH_PROGRESS_PROTOCOL": "1"})
    app.jobs.submit("selftest-translation", lambda: _execute_with_capture([str(python), "-u", "-c", child], child_env, task_id),
                    lambda _: task_manager.complete_task(task_id, "success"))
    intervals, samples, hidden_redraws = [], [], []
    previous = time.perf_counter()
    deadline = previous + 10

    def heartbeat():
        nonlocal previous
        now = time.perf_counter()
        intervals.append(now - previous)
        previous = now
        row = app._task_row_values.get(task_id) if hasattr(app, "_task_row_values") else None
        if row:
            value = int(row[2].rstrip("%"))
            if not samples or samples[-1] != value:
                samples.append(value)
        if now > deadline or (now - beginning > 2 and not app.jobs.busy() and 100 in samples):
            app.root.quit()
        else:
            app.root.after(10, heartbeat)

    def open_logs():
        hidden_redraws.append(len(insertions))
        app.log_level_var.set("全部")
        app.show_page("logs")

    app.root.after(10, heartbeat)
    app.root.after(750, open_logs)
    app.root.mainloop()
    gap_ms = max(intervals, default=0) * 1000
    assert gap_ms < 350, f"GUI heartbeat stalled for {gap_ms:.1f} ms"
    assert hidden_redraws == [0], f"Hidden log page was redrawn: {hidden_redraws}"
    assert len([value for value in samples if 0 < value < 100]) >= 2, samples
    assert 100 in samples, samples
    app.log_text.insert = original_insert
    return {"service_click_ms": round(click_ms, 2), "max_heartbeat_gap_ms": round(gap_ms, 2),
            "live_progress_samples": samples, "log_burst_lines": 5000, "hidden_log_redraws": 0}


def desktop_self_test():
    """Exercise the packaged GUI and hidden child pipeline without an API call."""
    import logging
    import tkinter as tk
    from flask import Flask
    from server import PDFTranslator
    from utils.execute import _execute_with_capture
    python = app_paths.kernel_python(home=HOME)
    if not python.exists():
        python = next((HOME / "core/python").glob("*/python.exe"))
    command = [str(python), "-u", "-c",
               "import sys; print('INFO self-test initialize'); print('WARNING self-test CPU fallback', file=sys.stderr); raise ImportError('self-test BitStream missing')"]
    try:
        _execute_with_capture(command, app_paths.child_env(), None)
        raise AssertionError("Expected child failure was not captured")
    except subprocess.CalledProcessError as exc:
        translator = PDFTranslator.__new__(PDFTranslator)
        translator.app = Flask("desktop-self-test")
        payload = translator._exception_payload(exc, context="自检模拟故障")
    assert payload["errorType"] == "ImportError", payload
    assert "BitStream" in payload["message"], payload
    assert {"INFO", "WARNING", "ERROR"}.issubset({item.level for item in APPLOG.snapshot()})
    from utils.process_utils import KernelTranslationError
    from runtime_progress import PREFIX
    error_line = PREFIX + json.dumps({'version': 1, 'type': 'error', 'progress': 92.44,
        'stage': 'Typesetting', 'errorType': 'StatisticsError', 'error': 'no mode for empty data'})
    try:
        _execute_with_capture([str(python), '-u', '-c', f'print({error_line!r})'], app_paths.child_env(), None)
        raise AssertionError('Fatal kernel event was lost because its CLI returned zero')
    except KernelTranslationError as exc:
        semantic_payload = translator._exception_payload(exc, context='自检模拟排版故障')
        assert exc.kernel_exit_code == 0
        assert semantic_payload['errorType'] == 'StatisticsError'
        assert 'no mode for empty data' in semantic_payload['message']

    class TestApp(LauncherApp):
        def refresh_kernel_status(self, silent=False):
            pass

        def refresh_cache_size(self):
            pass

    root = tk.Tk()
    root.withdraw()
    app = TestApp(root)
    app.log_level_var.set("ERROR")
    app.refresh_logs_view()
    displayed = app.log_text.get("1.0", "end")
    assert "BitStream" in displayed and "[WARNING]" not in displayed
    assert len(app.gpu_modes) == 3
    assert len(app.page_canvases) == 4
    from utils.task_manager import task_manager
    before = {key: var.get() for key, var in app.translation_vars.items()}
    app.apply_speed_preset(True)
    after = {key: var.get() for key, var in app.translation_vars.items()}
    assert {key for key in before if before[key] != after[key]} <= {"poolSize"}
    for key, value in before.items():
        app.translation_vars[key].set(value)
    check_task = "desktop-controls-self-test"
    task_manager.add_task(check_task, {"taskId": check_task, "fileName": "control-check.pdf", "active": True, "status": "queued", "progress": 0})
    for action, status in (("pause", "已暂停"), ("resume", "等待中")):
        task_manager.act(check_task, action)
        app.refresh_tasks()
        assert app.task_trees["active"].item(check_task, "values")[1] == status
    task_manager.act(check_task, "cancel")
    app.refresh_tasks()
    assert not app.task_trees["active"].exists(check_task)
    assert app.task_trees["history"].item(check_task, "values")[1] == "已取消"
    assert app.display["width"] <= app.root.winfo_screenwidth()
    import tkinter.messagebox as dialogs
    dialogs.showerror = dialogs.showinfo = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Unexpected runtime popup"))
    app._msg("自检", "通知已进入日志", error=True)
    performance = responsiveness_self_test(app, python)
    assert app.api_registry is not None, app.api_state.get()
    from utils.llm_ui import SelectBox
    fixed_fields = [widget for key, widget in app.translation_fields.items() if key != 'fontFamily' and isinstance(widget, SelectBox)]
    assert len(fixed_fields) == 5
    assert all(str(widget.cget("state")) in ("readonly", "disabled") for widget in fixed_fields)
    original_engine = app.translation_vars["engine"].get()
    for engine in ("pdf2zh", "pdf2zh_next"):
        app.translation_vars["engine"].set(engine)
        assert app.engine_sections[engine].winfo_manager() == "pack"
        assert all(not frame.winfo_manager() for key, frame in app.engine_sections.items() if key != engine)
    app.translation_vars["engine"].set(original_engine)
    assert app.footer_version.cget("text") == "v" + APP_VERSION
    assert app.upstream_link.url == "https://github.com/guaguastandup/zotero-pdf2zh"
    assert app.project_link.url == "https://github.com/erxinzi/PDF2zh"
    app.new_api()
    app.api_editor_vars["service"].set("deepseek")
    app._api_type_selected()
    app.api_editor_vars["model"].set("deepseek-v4-pro")
    app.reasoning_effort.set("max")
    app._reasoning_selected("effort")
    draft = app._editor_entry()
    assert draft["extraData"]["deepseek_thinking_mode"] == "enabled"
    assert draft["extraData"]["deepseek_reasoning_effort"] == "max"
    app.cancel_api_edit()
    root.update_idletasks()
    result = {"version": APP_VERSION, "hidden_child": True, "exception_payload": payload["message"],
              "log_levels": True, "gui_level_filter": True, "cuda_controls": True, "runtime_popups": False, "dpi": app.display["dpi"],
              "dpi_mode": __import__("utils.display", fromlist=["enable_high_dpi"]).enable_high_dpi(),
              "window_size": [app.display["width"], app.display["height"]], "scrollable_pages": len(app.page_canvases)}
    result["responsiveness"] = performance
    result.update({"quality_preset_preserved": True, "separate_task_history_tabs": True,
                   "task_pause_resume_cancel_states": True, "upload_and_translation_settings": True})
    result.update({"standalone_api_registry": True, "selection_only_basic_fields": len(fixed_fields),
                   "single_engine_provider_selector": True, "configured_provider_filter": True})
    result.update({"reasoning_controls": True, "attribution_links": True, "backend_plugin_protocol": True})
    from utils.plugin_install import plugin_path
    import zipfile
    with zipfile.ZipFile(plugin_path()) as archive:
        assert archive.testzip() is None
        plugin_manifest = json.loads(archive.read('manifest.json'))
        assert plugin_manifest['applications']['zotero']['id'] == 'pdf2zh@guaguastandup.com'
    assert app.plugin_button.master is app.upload_button.master
    assert app.api_settings_button.master is app.upload_button.master
    assert len(app.provider_boxes) == 1
    assert len(root._pdf2zh_check_images) == 4
    assert app.wheel_focus.tag in fixed_fields[0].bindtags()
    if os.name == 'nt':
        from utils.windows_drag import ShellFileData
        with ShellFileData(plugin_path()) as data:
            assert data.pointer.value
    result.update({"plugin_bundled": plugin_manifest['version'], "native_plugin_file_drag": True,
                   "click_before_wheel": True, "checkmark_indicators": True, "home_uses_translation_settings": True})
    assert app.server_btn.master is app.sidebar_status and app.kernel_dot.master is app.sidebar_status
    assert app.kernel_text.cget('text') == 'Core' and app.server_text.cget('text') == '服务状态'
    assert app.brand_logo.height() >= app.px(64)
    assert app.crop_settings.master.master is app.output_section
    assert int(app.crop_settings.master.grid_info()['column']) == 1
    assert len(app.home_output_controls) == len(app.open_output_controls) == 6
    assert len(app.home_layout_controls) == 2 and 'dualMode' not in app.translation_fields
    assert len(app.next_flags) == 11 and all(int(w.grid_info()['column']) in (0,1,2) for w in app.next_flags.values())
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    duplicated = {str(app.translation_vars[key]) for key in (*app.home_output_controls, 'dualMode', 'noMono', 'noDual')}
    assert all(str(widget.cget('variable')) not in duplicated for widget in descendants(app.page_bodies['translation'])
               if widget.winfo_class() in ('TCheckbutton','TRadiobutton'))
    result.update({'sidebar_service_status': True, 'two_column_translation_fields': True,
                   'three_column_engine_checks': True, 'crop_card_on_right': True,
                   'home_output_settings_authoritative': True, 'larger_brand_logo': True})
    assert app_paths.resource_path('reference_layout.py').is_file()
    result['reference_layout_adapter_bundled'] = True
    assert app_paths.resource_path('layout_integrity.py').is_file()
    result['layout_integrity_adapter_bundled'] = True
    result['fatal_kernel_event_with_zero_exit'] = True
    assert root.title() == 'PDF2zh' and len(app.window_icons) == 7
    result['window_title'] = root.title()
    result['custom_branding_icons'] = True
    app._close()
    (LOG_DIR / "self-test-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.getLogger(__name__).info("桌面打包自检通过")


def main():
    global APPLOG
    from utils.display import enable_high_dpi
    dpi_mode = enable_high_dpi()
    os.environ["ZOTERO_PDF2ZH_DESKTOP"] = "1"
    if "--self-test" in sys.argv:
        APPLOG = AppLogger(LOG_DIR / "self-test.log")
    install_desktop_logging(APPLOG)
    app_paths.apply_exe_patches()
    if "--self-test" in sys.argv:
        desktop_self_test()
        return
    log_system(f"启动 v{APP_VERSION}，DPI 模式 {dpi_mode}；CUDA 组件及其缓存仅保存在 core 内", "INFO")
    import tkinter as tk
    from tkinter import messagebox
    # 路径检查：中文 / 非专属目录 -> 提示后退出（按方案要求）
    ok, reason = app_paths.check_startup_path(HOME)
    if not ok:
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("路径不符合要求", reason)
            r.destroy()
        except Exception:
            pass
        # 用户点击确定后退出
        sys.exit(2)
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
