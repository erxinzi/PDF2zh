"""Project-local paths used by both the desktop and translation interpreter."""
from __future__ import annotations

import os
from pathlib import Path


def cuda_paths(root, *, create=True):
    project = Path(root).resolve()
    core = project / "core"
    if core.resolve() != core:
        raise ValueError("core 目录不能通过符号链接或目录联接指向其他位置")
    paths = {
        "core": core,
        "target": core / "onnxruntime-cuda",
        "download_cache": core / "cuda-download-cache",
        "temporary": core / "cuda-tmp",
        "profile": core / "cuda-profile",
        "compute_cache": core / "cuda-cache" / "compute",
        "python": core / "python",
    }
    for name, path in paths.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(core):
            raise ValueError(f"CUDA {name} 路径必须位于项目 core 目录内：{path}")
    if create:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
    return paths


def cuda_environment(root, source=None):
    """Return child-only settings; never edit the parent or machine environment."""
    paths = cuda_paths(root)
    env = dict(os.environ if source is None else source)
    for key in list(env):
        upper = key.upper()
        if upper.startswith(("UV_", "PIP_", "CUDA_", "CUDNN_")) or upper in {
            "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
        }:
            del env[key]
    profile = paths["profile"]
    env.update({
        "UV_CACHE_DIR": str(paths["download_cache"]),
        "UV_PYTHON_INSTALL_DIR": str(paths["python"]),
        "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "true", "UV_LINK_MODE": "copy",
        "PIP_CACHE_DIR": str(paths["download_cache"] / "pip"),
        "PIP_CONFIG_FILE": os.devnull,
        "TEMP": str(paths["temporary"]), "TMP": str(paths["temporary"]), "TMPDIR": str(paths["temporary"]),
        "CUDA_CACHE_PATH": str(paths["compute_cache"]),
        "CUDA_PATH": str(paths["target"] / "nvidia/cuda_runtime"),
        "CUDA_HOME": str(paths["target"] / "nvidia/cuda_runtime"),
        "CUDNN_PATH": str(paths["target"] / "nvidia/cudnn"),
        "PYTHONUSERBASE": str(profile / "python-user"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "XDG_CACHE_HOME": str(paths["core"] / "cuda-cache"),
        "XDG_CONFIG_HOME": str(profile / "config"), "XDG_DATA_HOME": str(profile / "data"),
    })
    if os.name == "nt":
        env.update({"USERPROFILE": str(profile), "APPDATA": str(profile / "AppData/Roaming"), "LOCALAPPDATA": str(profile / "AppData/Local")})
    # Preserve OS/driver tools, excluding a system CUDA Toolkit from this child.
    kept = []
    for raw in env.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        if "cuda" in raw.lower() or "cudnn" in raw.lower():
            if not Path(raw).resolve().is_relative_to(paths["core"]):
                continue
        kept.append(raw)
    env["PATH"] = os.pathsep.join(kept)
    return env


def require_project_python(root, python):
    paths = cuda_paths(root, create=False)
    resolved = Path(python).resolve()
    if not resolved.is_relative_to(paths["core"]) or not resolved.is_file():
        raise ValueError("CUDA 组件只能使用项目 core 内的 Python 安装")
    return resolved
