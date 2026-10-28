"""Optional CUDA components and translation runtime command construction."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from runtime_isolation import cuda_environment, cuda_paths, require_project_python

from utils.process_utils import run_process, stream_process


GPU_REQUIREMENT = "onnxruntime-gpu[cuda,cudnn]==1.23.2"
GPU_REQUIREMENTS = [GPU_REQUIREMENT, "nvidia-cudnn-cu12==9.10.2.21"]
DEFAULTS = {"device": "auto", "gpu_id": 0, "gpu_memory_mb": 2048}


def load_gpu_settings(root):
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads((Path(root) / "settings.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    mode = settings.get("device", "auto")
    if mode not in ("auto", "cpu", "cuda"):
        raise ValueError("计算模式必须为 auto、cpu 或 cuda")
    gpu_id, memory = int(settings["gpu_id"]), int(settings["gpu_memory_mb"])
    if gpu_id < 0 or not 256 <= memory <= 65536:
        raise ValueError("GPU 编号不能为负数，显存上限应为 256–65536 MB")
    return {"device": mode, "gpu_id": gpu_id, "gpu_memory_mb": memory}


def runtime_env(root, env=None, *, probe=False):
    root = Path(root)
    out = cuda_environment(root, env)
    settings = load_gpu_settings(root)
    out.update({
        "ZOTERO_PDF2ZH_APP_HOME": str(root),
        "PDF2ZH_DEVICE": "cuda" if probe else settings["device"],
        "PDF2ZH_GPU_ID": str(settings["gpu_id"]),
        "PDF2ZH_GPU_MEMORY_MB": str(settings["gpu_memory_mb"]),
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        "PDF2ZH_PROGRESS_PROTOCOL": "1", "TQDM_DISABLE": "1",
        "NO_COLOR": "1", "FORCE_COLOR": "0", "FORCE_TERMINAL": "0",
    })
    # BabelDOC and pdf2zh-next ignore XDG paths and use the user profile.
    # Scope this change to the kernel child; never change the system profile.
    if not probe:
        profile = root / "cache" / "runtime-profile"
        profile.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            out["USERPROFILE"] = str(profile)
    return out


def bootstrap_path():
    import app_paths
    return app_paths.resource_path("runtime_bootstrap.py")


def prepare_runtime_command(command, env):
    stem = Path(command[0]).stem.lower()
    if stem in ("pdf2zh_next", "pdf2zh2"):
        executable = Path(command[0])
        python = executable.with_name("python.exe" if os.name == "nt" else "python")
        arguments = command[1:]
    elif len(command) > 2 and command[1:3] == ["-m", "pdf2zh_next"]:
        python, arguments = Path(command[0]), command[3:]
    else:
        return command, env
    if not python.exists():
        raise RuntimeError(f"找不到翻译内核解释器：{python}")
    root = Path(env["ZOTERO_PDF2ZH_APP_HOME"])
    return [str(python), "-u", str(bootstrap_path()), *arguments], runtime_env(root, env)


def detect_nvidia():
    command = shutil.which("nvidia-smi")
    if not command:
        return "未检测到 nvidia-smi；CPU 模式仍可正常翻译"
    result = run_process([command, "--query-gpu=index,name,driver_version,memory.total", "--format=csv,noheader"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "无法读取 NVIDIA 驱动")
    return result.stdout.strip()


def probe_cuda(root, python):
    env = runtime_env(root, probe=True)
    return stream_process([str(python), "-u", str(bootstrap_path()), "--probe-cuda"], env=env, timeout=90)


def install_cuda(root, python, index_url=None):
    import app_paths
    root = Path(root).resolve()
    python = require_project_python(root, python)
    uv = next((path for path in (app_paths.resource_path("_tools", "uv.exe"), root / "_tools/uv.exe") if path.is_file()), None)
    if uv is None:
        raise RuntimeError("找不到随包提供的 uv 下载器")
    paths = cuda_paths(root)
    target = paths["target"]
    marker = target / "ready.json"
    # Never mark an incomplete target as usable by new translation jobs.
    marker.unlink(missing_ok=True)
    command = [str(uv), "--no-config", "--no-python-downloads", "pip", "install", "--python", str(python), "--target", str(target), "--reinstall", "--link-mode", "copy", *GPU_REQUIREMENTS]
    if index_url:
        command.extend(["--index-url", index_url])
    logging.getLogger("pdf2zh.kernel").info("开始安装 CUDA 加速组件及 CUDA/cuDNN 运行库，文件仅保存在 %s", target)
    stream_process(command, env=cuda_environment(root), timeout=1800)
    marker.write_text(json.dumps({"requirements": GPU_REQUIREMENTS}), encoding="utf-8")
    try:
        probe_cuda(root, python)
    except Exception:
        marker.unlink(missing_ok=True)
        raise
    return target
