# -*- coding: utf-8 -*-
"""EXE centralized paths + sandbox.

目标（来自 exe打包方案.md + 用户要求）：
1. 瘦 exe：内置最基本的运行环境（PyInstaller 冻结的 Python + Flask 等），
   重型翻译内核（pdf2zh-next 等）首次按需联网下载到 exe 目录下的 core/。
2. 禁止向 exe 所在文件夹以外的地方安装运行环境或存放文件。
   所有可写目录（core / config / translated / cache）全部收敛到 exe 目录。
3. 启动时检查路径：中文路径 / 非专属目录 -> 提示后退出。

源码运行时（python server.py）保持原行为：APP_HOME = server/ 目录。
冻结运行时（PyInstaller exe）APP_HOME = exe 所在目录，资源文件走 _MEIPASS。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from utils.process_utils import run_process

APP_NAME = "zotero-pdf2zh-server"
CORE_DIRNAME = "core"
CACHE_DIRNAME = "cache"
CONFIG_DIRNAME = "config"
OUTPUT_DIRNAME = "translated"
LOG_DIRNAME = "logs"


def branding_path(name):
    """Use bundled icons in the EXE and the shared branding source in a checkout."""
    bundled = resource_path('branding', name)
    if getattr(sys, 'frozen', False) or bundled.is_file():
        return bundled
    return Path(__file__).resolve().parent / 'assets/branding' / name

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> Path:
    """可写根目录：冻结时 = exe 所在目录；源码时 = server/ 目录。"""
    if is_frozen():
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return Path(os.path.dirname(os.path.abspath(__file__)))


def resource_base() -> Path:
    """只读资源根目录：冻结时 = PyInstaller _MEIPASS；源码时 = server/。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # server/app_paths.py -> server/
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return exe_dir()


def resource_path(*parts: str) -> Path:
    return resource_base().joinpath(*parts)


def _load_settings(home: Path) -> dict:
    p = home / "settings.json"
    try:
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            return v if isinstance(v, dict) else {}
    except Exception:
        pass
    return {}


def core_dir(home: Path | None = None) -> Path:
    home = home or exe_dir()
    custom = _load_settings(home).get("core_dir")
    if custom:
        try:
            c = Path(str(custom))
            if not c.is_absolute():
                c = home / c
            return c
        except Exception:
            pass
    return home / CORE_DIRNAME


def cache_dir(home: Path | None = None) -> Path:
    return (home or exe_dir()) / CACHE_DIRNAME


def config_dir(home: Path | None = None) -> Path:
    return (home or exe_dir()) / CONFIG_DIRNAME


def output_dir(home: Path | None = None) -> Path:
    return (home or exe_dir()) / OUTPUT_DIRNAME


def logs_dir(home: Path | None = None) -> Path:
    return (home or exe_dir()) / LOG_DIRNAME


def ensure_dirs(home: Path | None = None) -> dict[str, Path]:
    home = home or exe_dir()
    paths = {
        "home": home,
        "core": core_dir(home),
        "cache": cache_dir(home),
        "config": config_dir(home),
        "output": output_dir(home),
        "logs": logs_dir(home),
        "uv_cache": cache_dir(home) / "uv-cache",
        "pip_cache": cache_dir(home) / "pip-cache",
        "hf_home": cache_dir(home) / "huggingface",
        "torch_home": cache_dir(home) / "torch",
        "tmp": cache_dir(home) / "tmp",
        "uv_python": core_dir(home) / "python",
    }
    for key in ("core", "cache", "config", "output", "logs", "uv_cache", "pip_cache",
                "hf_home", "torch_home", "tmp", "uv_python"):
        try:
            paths[key].mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return paths


def setup_sandbox(home: Path | None = None) -> dict[str, Path]:
    """把所有第三方工具的缓存/数据目录钉死在 exe 目录内。

    覆盖：uv / pip / HuggingFace / torch / XDG / python standalone 安装位。
    必须在任何子进程（uv/pip/pdf2zh_next）启动之前调用。
    conda 一律禁用（conda 的 envs 目录天生在 exe 之外），只用 uv。
    """
    _configure_console()
    paths = ensure_dirs(home)
    home = paths["home"]
    env = os.environ
    env["ZOTERO_PDF2ZH_APP_HOME"] = str(home)
    env["ZOTERO_PDF2ZH_EXE_MODE"] = "1" if is_frozen() else "0"
    # --- 包管理器缓存 ---
    env["UV_CACHE_DIR"] = str(paths["uv_cache"])
    env["PIP_CACHE_DIR"] = str(paths["pip_cache"])
    env["UV_PYTHON_INSTALL_DIR"] = str(paths["uv_python"])
    # uv 新版本也认 XDG / 本地数据目录，一并钉死
    env["XDG_CACHE_HOME"] = str(paths["cache"])
    env["XDG_DATA_HOME"] = str(paths["cache"] / "share")
    env["XDG_CONFIG_HOME"] = str(paths["cache"] / "xdg-config")
    # --- 模型 / 权重缓存 ---
    env["HF_HOME"] = str(paths["hf_home"])
    env["HUGGINGFACE_HUB_CACHE"] = str(paths["hf_home"] / "hub")
    env["HF_HUB_CACHE"] = str(paths["hf_home"] / "hub")
    env["TORCH_HOME"] = str(paths["torch_home"])
    env["MODELSCOPE_CACHE"] = str(paths["cache"] / "modelscope")
    # --- 临时文件：exe 模式全部钉到 exe 内 tmp；源码模式保留系统 TEMP ---
    env["ZOTERO_PDF2ZH_TMPDIR"] = str(paths["tmp"])
    if is_frozen():
        env["TMP"] = str(paths["tmp"])
        env["TEMP"] = str(paths["tmp"])
        env["TMPDIR"] = str(paths["tmp"])
    # NOTE: 源码模式不碰全局 TEMP/TMP；exe 模式已在上面钉到 exe 内。
    # --- python 隔离 ---
    env["PYTHONNOUSERSITE"] = "1"
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # --- 禁用 conda：从 PATH 里摘掉，避免误用外部 conda ---
    # （只影响当前进程及子进程，不改系统配置）
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    kept: list[str] = []
    for p in parts:
        low = p.lower()
        if "miniconda" in low or "anaconda" in low or "micromamba" in low:
            continue
        kept.append(p)
    env["PATH"] = os.pathsep.join(kept)
    env["CONDA_SHLVL"] = ""
    # --- 自带 uv：exe/_tools 与 _MEIPASS/_tools 优先进 PATH ---
    try:
        _tools_dirs = [str(home / "_tools"), str(resource_base() / "_tools")]
        _cur = [x for x in env.get("PATH", "").split(os.pathsep) if x]
        for _td in _tools_dirs:
            try:
                import pathlib as _pl
                _has_uv = (_pl.Path(_td) / ("uv.exe" if os.name == "nt" else "uv")).exists()
            except Exception:
                _has_uv = False
            if _has_uv and _td not in _cur:
                _cur.insert(0, _td)
        env["PATH"] = os.pathsep.join(_cur)
    except Exception:
        pass
    return paths


def child_env(extra: dict | None = None) -> dict:
    """给翻译子进程用的环境：在 setup_sandbox 基础上再把 TMP 钉到 exe 内。"""
    env = dict(os.environ)
    tmp = env.get("ZOTERO_PDF2ZH_TMPDIR")
    if tmp:
        env["TMP"] = tmp
        env["TEMP"] = tmp
        env["TMPDIR"] = tmp
    if extra:
        env.update(extra)
    return env


# ---------------- 路径检查 ----------------

_SHARED_DIR_NAMES = {
    "desktop", "downloads", "documents", "mydocuments",
    "桌面", "下载", "文档",
}


def check_startup_path(home: Path | None = None) -> tuple[bool, str]:
    """返回 (ok, reason)。ok=False 时调用方应弹窗提示 reason 并退出。

    规则（按方案要求）：
    1. 路径含中文/CJK -> 不允许（uv/python/pdf2zh 对中文路径支持差）。
    2. 放在非专属目录 -> 不允许。包括：桌面/下载/文档根、盘符根、
       Windows/Program Files/系统目录、以及文件杂乱的共享目录。
    """
    home = home or exe_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    hs = str(home)
    # 1) 中文 / CJK
    if _CJK_RE.search(hs):
        return False, (
            "当前路径包含中文字符：\n" + hs +
            "\n\n为避免翻译内核（uv / Python / pdf2zh）因中文路径出错，"
            "请将本软件移动到纯英文路径的专属文件夹后再打开。\n例如：D:\\zotero-pdf2zh-server\\"
        )
    # 2a) 盘符根目录
    try:
        if Path(hs).parent == Path(hs).anchor and Path(hs).anchor:
            # 如 D:\ 这种（anchor == path）
            if os.path.normcase(hs) == os.path.normcase(Path(hs).anchor):
                return False, (
                    "请不要把 exe 直接放在盘符根目录：\n" + hs +
                    "\n\n请新建一个专属英文文件夹（例如 D:\\zotero-pdf2zh-server\\），"
                    "把 exe 放进去后再运行。"
                )
    except Exception:
        pass
    # 2b) 常见共享目录
    try:
        lname = home.name.strip().lower()
        # 用户桌面/下载/文档根
        userprofile = os.environ.get("USERPROFILE", "")
        special = set()
        for sub in ("Desktop", "Downloads", "Documents", "Downloads", "OneDrive"):
            if userprofile:
                special.add(os.path.normcase(os.path.join(userprofile, sub)))
        special |= {
            os.path.normcase(os.path.expanduser("~")),
            os.path.normcase("C:\\"), os.path.normcase("D:\\"),
            os.path.normcase("C:\\Windows"), os.path.normcase("C:\\Program Files"),
            os.path.normcase("C:\\Program Files (x86)"),
        }
        if os.path.normcase(hs) in special or lname in _SHARED_DIR_NAMES:
            return False, (
                "检测到 exe 被放在共享/系统目录：\n" + hs +
                "\n\n请新建一个专属英文文件夹（例如 D:\\zotero-pdf2zh-server\\），"
                "把 exe 放进去后再运行。\n内核、配置、缓存只会存放在 exe 所在文件夹内。"
            )
    except Exception:
        pass
    # 2c) 专属目录启发式（仅 exe 模式；源码开发时跳过）
    # exe 同级无关文件太多 -> 疑似共用目录，提醒
    try:
        if home.exists():
            entries = list(home.iterdir())
            # 允许的文件：exe、settings.json、core/cache/config/translated、日志
            allowed = {CORE_DIRNAME.lower(), CACHE_DIRNAME.lower(),
                       CONFIG_DIRNAME.lower(), OUTPUT_DIRNAME.lower(),
                       "settings.json", "server.log", "launcher.log"}
            allow_ext = {".exe", ".log", ".json", ".pdf"}
            strangers = 0
            for e in entries:
                n = e.name.lower()
                if n in allowed:
                    continue
                if e.is_dir():
                    strangers += 3  # 子目录权重更高
                elif e.suffix.lower() in allow_ext:
                    continue
                else:
                    strangers += 1
            if is_frozen() and strangers >= 15:
                return False, (
                    "检测到 exe 被放在非专属目录（同级有大量无关文件）：\n" + hs +
                    "\n\n请新建一个专属英文文件夹，把 exe 放进去后再运行，"
                    "以免内核/缓存与其它文件混杂。"
                )
    except Exception:
        pass
    # 2d) 写权限
    try:
        probe = home / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception:
        return False, (
            "当前目录没有写入权限：\n" + hs +
            "\n\n内核、配置、缓存都需要写在 exe 所在文件夹内，"
            "请换到有写入权限的专属英文文件夹，或右键“以管理员身份运行”。"
        )
    return True, ""


_CONFIG_EXAMPLES = ("config.json.example", "config.toml.example", "venv.json.example")


def seed_config_examples(home: Path | None = None) -> list[str]:
    """把只读模板（_MEIPASS/config/*.example）播种到 exe 内 config/。

    utils.config_migration 只认“active 文件同级的 .example”，而冻结后模板
    在 _MEIPASS 里，不播种的话 prepare_config_files 会报“缺少配置文件及模板”。
    已存在的模板绝不覆盖。返回本次播种的文件名。
    """
    import shutil as _sh
    home = home or exe_dir()
    dest_dir = config_dir(home)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    src_dir = resource_base() / "config"
    seeded: list[str] = []
    for name in _CONFIG_EXAMPLES:
        dest = dest_dir / name
        try:
            if dest.exists():
                continue
            src = src_dir / name
            if not src.exists():
                continue
            _sh.copy2(str(src), str(dest))
            seeded.append(name)
        except Exception:
            pass
    return seeded


def describe_paths(home: Path | None = None) -> str:
    home = home or exe_dir()
    lines = [
        f"软件目录: {home}",
        f"内核目录: {core_dir(home)}  (pdf2zh-next 按需下载到此)",
        f"配置目录: {config_dir(home)}",
        f"输出目录: {output_dir(home)}",
        f"缓存目录: {cache_dir(home)}  (uv/pip/模型缓存全部在此)",
        f"日志目录: {logs_dir(home)}  (运行日志只在此)",
        "冻结模式: " + ("是 (exe)" if is_frozen() else "否 (源码 python 运行)"),
    ]
    return "\n".join(lines)

# ---------------- 内核定位（core/ 内） ----------------

def kernel_venv_dir(engine: str = "pdf2zh_next", home: Path | None = None) -> Path:
    """内核 venv 目录：<home>/core/<venv-name>。"""
    from pathlib import Path as _P
    names = {
        "pdf2zh_next": "zotero-pdf2zh-next-venv",
        "pdf2zh": "zotero-pdf2zh-venv",
    }
    return core_dir(home) / names.get(engine, names["pdf2zh_next"])


def kernel_python(engine: str = "pdf2zh_next", home: Path | None = None) -> Path:
    """内核 python 可执行文件路径（不存在则返回期望位置）。"""
    import platform as _platform
    d = kernel_venv_dir(engine, home)
    if _platform.system() == "Windows":
        for c in (d / "Scripts" / "python.exe", d / "python.exe"):
            if c.exists():
                return c
        return d / "Scripts" / "python.exe"
    for c in (d / "bin" / "python", d / "bin" / "python3"):
        if c.exists():
            return c
    return d / "bin" / "python"


def kernel_status(engine: str = "pdf2zh_next", home: Path | None = None) -> dict:
    """轻量内核状态：不导入重型模块，只看 venv + 包版本。"""
    import subprocess as _sp
    home = home or exe_dir()
    vdir = kernel_venv_dir(engine, home)
    py = kernel_python(engine, home)
    info: dict = {"engine": engine, "venv_dir": str(vdir),
                  "python": str(py), "exists": bool(py.exists()),
                  "versions": {}, "ok": False}
    if not py.exists():
        return info
    try:
        names = ["pdf2zh-next", "BabelDOC", "PyMuPDF", "pypdf"] if engine == "pdf2zh_next" \
            else ["pdf2zh", "BabelDOC", "PyMuPDF", "pypdf"]
        code = (
            "import json; from importlib.metadata import version, PackageNotFoundError; "
            f"names={names!r}; out={{}};\nfor n in names:\n"
            "    [out.__setitem__(n, version(n)) if True else None][0] if False else None\n"
            "print(json.dumps(out))"
        )
        # 上面写法绕，直接用简单循环代码
        code = (
            "import json\nfrom importlib.metadata import version, PackageNotFoundError\n"
            f"names={names!r}\nout={{}}\nfor n in names:\n"
            "    try:\n        out[n]=version(n)\n"
            "    except PackageNotFoundError:\n        out[n]=None\n"
            "print(json.dumps(out))"
        )
        r = run_process([str(py), "-I", "-c", code], capture_output=True, text=True,
                    timeout=60, env=child_env())
        if r.returncode == 0 and r.stdout.strip():
            info["versions"] = json.loads(r.stdout.strip())
            main_pkg = "pdf2zh-next" if engine == "pdf2zh_next" else "pdf2zh"
            info["ok"] = bool(info["versions"].get(main_pkg))
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def apply_exe_patches() -> None:
    """把旧源码里写死到源码目录 / 系统目录的行为，全部重定向到 exe 目录。

    只在冻结（exe）时强制；源码运行时保持原行为（方便开发调试）。
    幂等，可重复调用。
    """
    if getattr(apply_exe_patches, "_done", False):
        return
    apply_exe_patches._done = True  # type: ignore[attr-defined]
    home = exe_dir()
    setup_sandbox(home)
    if not is_frozen():
        return
    # --- lifecycle：数据目录钉到 exe 内，模板读 _MEIPASS ---
    try:
        import utils.environment_lifecycle as LC  # type: ignore
        from pathlib import Path as _P
        _res_example = resource_path("config", "venv.json.example")
        if _res_example.exists():
            LC.DEFAULT_VENV_CONFIG = _res_example
        _orig_find = LC.find_existing_environment

        def _patched_find(engine: str, env_tool: str = "auto"):
            # 优先 exe/core 下的新位置
            try:
                names = getattr(LC, "ENGINE_ENV_NAMES", {})
                env_name = names.get(engine, "")
                if env_tool in {"auto", "uv"} and env_name:
                    cand = kernel_venv_dir(engine, home)
                    py = kernel_python(engine, home)
                    if py.exists():
                        return "uv", _P(cand), _P(py)
            except Exception:
                pass
            # 兼容旧位置：exe 根目录（历史版本直接放 exe 根）
            try:
                r = _orig_find(engine, env_tool)
                if r is not None:
                    return r
            except Exception:
                pass
            return None

        LC.find_existing_environment = _patched_find  # type: ignore
        # --- exe 专用 venv 创建：显式 provision + 完整日志 + 本机回退 ---
        # 背景：uv venv --python 3.12 在只有 Python 3.14 的机器上需要联网下载
        # 3.12 解释器（python-build-standalone，走 GitHub），国内直连经常失败
        # exit=1 且旧代码吞掉了 stderr。wrapper 把每一步输出都打印到日志页，
        # 下载失败时自动回退本机 Python，保证张三李四的机器都能装上内核。
        _orig_create_uv = LC._create_uv_environment

        def _exe_create_uv_environment(path, python_version):
            import shutil as _sh
            import subprocess as _sp
            uv_path = _sh.which("uv")
            if not uv_path:
                raise RuntimeError("未找到 uv（exe 自带的 _tools/uv.exe 缺失？请重新解压分发包）。")
            try:
                _ex = LC.resolve_environment_python(
                    _P(path) if not isinstance(path, _P) else path)
                if _ex.exists():
                    return _ex
            except Exception:
                pass
            try:
                _v = run_process([uv_path, "--version"], capture_output=True, text=True,
                             timeout=30, env=LC._package_manager_env())
                print("[内核] uv: " + ((_v.stdout or "") + (_v.stderr or "")).strip())
            except Exception as _e:
                print(f"[内核] uv --version 失败: {_e}")
            # 0) 自带解释器（随包 core/python）：有则离线直用，不碰网络
            _bundled = []
            try:
                _broot = os.path.join(str(home), "core", "python")
                if os.path.isdir(_broot):
                    for _dp, _dn, _fn in os.walk(_broot):
                        if "python.exe" in _fn:
                            _cand = os.path.join(_dp, "python.exe")
                            if _cand not in _bundled:
                                _bundled.append(_cand)
                        try:
                            _rel = os.path.relpath(_dp, _broot)
                            _depth = 0 if _rel == "." else _rel.count(os.sep)
                        except Exception:
                            _depth = 0
                        if _depth >= 4:
                            _dn[:] = []
                if _bundled:
                    print("[内核] 发现自带 Python（随包，离线可用）:")
                    for _b in _bundled:
                        print("  " + _b)
            except Exception as _e:
                print(f"[内核] 扫描自带 Python 失败: {_e}")
            if not _bundled:
                try:
                    print(f"[内核] 无自带解释器，联网准备 Python {python_version}…")
                    _r = run_process([uv_path, "python", "install", str(python_version)],
                                 cwd=str(home), capture_output=True, text=True,
                                 timeout=1200, env=LC._package_manager_env())
                    _out = (((_r.stdout or "") + chr(10) + (_r.stderr or "")).strip())[-4000:]
                    if _out:
                        print("[内核] uv python install 输出:" + chr(10) + _out)
                    if _r.returncode != 0:
                        print(f"[内核] uv python install 返回码 {_r.returncode}"
                              "（常见原因：GitHub 被墙，解释器下载失败），继续尝试 venv。")
                except Exception as _e:
                    print(f"[内核] uv python install 异常: {_e}")
            else:
                print("[内核] 使用自带解释器，跳过联网下载。")
            _specs = list(_bundled) + [str(python_version)]
            try:
                _f = run_process([uv_path, "python", "find"], capture_output=True, text=True,
                             timeout=60, env=LC._package_manager_env())
                if _f.returncode == 0:
                    for _line in reversed((_f.stdout or "").splitlines()):
                        _cand = _line.strip().strip(chr(34))
                        if _cand and os.path.exists(_cand) and _cand not in _specs:
                            _specs.append(_cand)
                            break
            except Exception:
                pass
            for _w in (_sh.which("python"), _sh.which("python3")):
                if _w and _w not in _specs:
                    _specs.append(_w)
            for _fb in ("3.14", "3"):
                if _fb not in _specs:
                    _specs.append(_fb)
            _last_err = ""
            for _spec in _specs:
                try:
                    print(f"[内核] 尝试创建 venv（--python {_spec}）…")
                    _sh.rmtree(str(path), ignore_errors=True)
                    _r = run_process([uv_path, "venv", str(path), "--python", str(_spec)],
                                 cwd=str(home), capture_output=True, text=True,
                                 timeout=1200, env=LC._package_manager_env())
                    _tail = (((_r.stdout or "") + chr(10) + (_r.stderr or "")).strip())[-4000:]
                    if _r.returncode == 0:
                        if _tail:
                            print("[内核] uv venv 输出:" + chr(10) + _tail)
                        _py = LC.resolve_environment_python(
                            _P(path) if not isinstance(path, _P) else path)
                        if _py.exists():
                            if str(_spec) != str(python_version):
                                print(f"[内核] 注意：Python {python_version} 不可用，"
                                      f"已回退到 {_spec} 创建内核；如翻译异常再装 3.12 重试。")
                            return _py
                        raise RuntimeError(f"uv venv 成功但找不到 Python: {path}")
                    _last_err = _tail or f"exit={_r.returncode}"
                    print(f"[内核] --python {_spec} 失败（exit={_r.returncode}）:"
                          + chr(10) + _last_err)
                except Exception as _e:
                    _last_err = f"{type(_e).__name__}: {_e}"
                    print(f"[内核] --python {_spec} 异常: {_last_err}")
            raise RuntimeError(
                "内核 venv 创建失败。最后一次错误:" + chr(10) + (_last_err or "unknown")
                + chr(10) + "解决办法：(1) 安装 Python 3.12 后重试，uv 会直接复用本机 3.12；"
                "(2) 检查网络/代理后重试。")

        LC._create_uv_environment = _exe_create_uv_environment  # type: ignore

        _orig_ensure = LC._ensure_canonical_environment

        def _patched_ensure(engine: str, env_tool: str, python_version: str):
            if env_tool == "uv" or env_tool == "auto":
                # 一律建到 core/ 内，禁用 conda（conda 天生写盘符外）
                from pathlib import Path as _P2
                env_dir = _P2(kernel_venv_dir(engine, home))
                py = LC._create_uv_environment(env_dir, python_version)
                return env_dir, py
            raise RuntimeError("exe 模式仅支持 uv 内核（conda 会写到 exe 目录之外，已禁用）。")

        LC._ensure_canonical_environment = _patched_ensure  # type: ignore

        _orig_cleanup = LC._cleanup_legacy_sidecar_environments

        def _patched_cleanup(engine: str, env_tool: str) -> None:
            try:
                import shutil as _sh
                names = getattr(LC, "ENGINE_ENV_NAMES", {})
                n = names.get(engine, "")
                if n:
                    _sh.rmtree(str(core_dir(home) / f"{n}.staging"), ignore_errors=True)
                    _sh.rmtree(str(core_dir(home) / f"{n}.backup"), ignore_errors=True)
            except Exception:
                pass
            try:
                _orig_cleanup(engine, env_tool)
            except Exception:
                pass

        LC._cleanup_legacy_sidecar_environments = _patched_cleanup  # type: ignore
        # SERVER_ROOT：涉及 state/config 的读写 -> exe 目录
        try:
            LC.SERVER_ROOT = _P(home)
        except Exception:
            pass
        # 子进程环境：TMP/TEMP/TMPDIR 钉到 exe 内（纵深防御）
        try:
            _orig_managed = LC.managed_python_env

            def _patched_managed(source=None):
                env = _orig_managed(source)
                try:
                    _tmp = os.environ.get("ZOTERO_PDF2ZH_TMPDIR")
                    if _tmp:
                        env["TMP"] = _tmp
                        env["TEMP"] = _tmp
                        env["TMPDIR"] = _tmp
                except Exception:
                    pass
                return env

            LC.managed_python_env = _patched_managed
        except Exception as _exc:
            print(f"[app_paths] managed env 补丁跳过: {_exc}")
    except Exception as exc:
        print(f"[app_paths] lifecycle 补丁跳过: {exc}")
    # --- venv.py：同理禁用 conda 发现 ---
    try:
        import utils.venv as V  # type: ignore
        _orig_tools = V.VirtualEnvManager._preferred_tools

        def _patched_tools(self):
            return ["uv"]

        V.VirtualEnvManager._preferred_tools = _patched_tools  # type: ignore
        _orig_install_tools = V.VirtualEnvManager._install_tools

        def _patched_install_tools(self):
            return ["uv"]

        V.VirtualEnvManager._install_tools = _patched_install_tools  # type: ignore
    except Exception as exc:
        print(f"[app_paths] venv 补丁跳过: {exc}")


def _configure_console() -> None:
    import platform as _plat
    if _plat.system() != 'Windows':
        return
    for _stream in (sys.stdout, sys.stderr):
        _rec = getattr(_stream, 'reconfigure', None)
        if callable(_rec):
            try:
                _rec(errors='replace')
            except Exception:
                pass
