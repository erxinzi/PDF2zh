"""Run in the translation Python, never in the frozen desktop interpreter."""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from runtime_isolation import cuda_paths
from runtime_progress import ProgressEmitter


LOG = logging.getLogger("pdf2zh.acceleration")
_DLL_HANDLES = []


def loaded_cuda_modules():
    """Read module paths from Windows directly, including their actual drive."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.K32EnumProcessModules.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    modules = (wintypes.HMODULE * 4096)()
    needed = wintypes.DWORD()
    if not kernel.K32EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed)):
        raise RuntimeError("无法核对当前进程 CUDA 动态库来源")
    if needed.value > ctypes.sizeof(modules):
        raise RuntimeError("动态库列表超出检测容量")
    prefixes = ("cudart", "cudnn", "cublas", "cufft", "curand", "nvrtc", "nvjitlink")
    result = set()
    for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
        path = ctypes.create_unicode_buffer(32768)
        if kernel.GetModuleFileNameW(module, path, len(path)) and Path(path.value).name.lower().startswith(prefixes):
            result.add(str(Path(path.value).resolve()))
    return sorted(result)


def hide_descendant_windows():
    if os.name == "nt":
        original = subprocess.Popen

        class HiddenPopen(original):
            def __init__(self, *args, **kwargs):
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
                super().__init__(*args, **kwargs)

        subprocess.Popen = HiddenPopen
        # multiprocessing starts its Windows children through _winapi directly.
        import _winapi
        original_create = _winapi.CreateProcess

        def create_hidden(application, command, process_attrs, thread_attrs, inherit_handles, flags, env, cwd, startup):
            return original_create(application, command, process_attrs, thread_attrs, inherit_handles,
                                   flags | subprocess.CREATE_NO_WINDOW, env, cwd, startup)

        _winapi.CreateProcess = create_hidden


def load_runtime():
    root = Path(os.environ["ZOTERO_PDF2ZH_APP_HOME"])
    mode = os.environ.get("PDF2ZH_DEVICE", "auto")
    addon = cuda_paths(root, create=False)["target"]
    using_addon = False
    if mode != "cpu" and (addon / "ready.json").exists():
        # Keep CPU and GPU wheels in separate directories: both export
        # onnxruntime, so installing them into one site-packages corrupts it.
        sys.path.append(str(addon))
        spec = importlib.util.spec_from_file_location(
            "onnxruntime", addon / "onnxruntime/__init__.py",
            submodule_search_locations=[str(addon / "onnxruntime")],
        )
        try:
            ort = importlib.util.module_from_spec(spec)
            sys.modules["onnxruntime"] = ort
            spec.loader.exec_module(ort)
            using_addon = True
        except Exception as exc:
            LOG.warning("GPU 运行时加载失败，改用 CPU 运行时：%s", exc)
            for name in list(sys.modules):
                if name == "onnxruntime" or name.startswith("onnxruntime."):
                    del sys.modules[name]
            sys.path.remove(str(addon))
            import onnxruntime as ort
    else:
        import onnxruntime as ort
    ort._pdf2zh_cuda_ready = False
    if mode != "cpu" and using_addon and "CUDAExecutionProvider" in ort.get_available_providers():
        try:
            if os.name == "nt":
                directories = list(addon.glob("nvidia/**/bin"))
                for directory in directories:
                    if not directory.resolve().is_relative_to(addon.resolve()):
                        raise RuntimeError(f"CUDA 动态库目录超出项目 core：{directory}")
                    _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
                os.environ["PATH"] = os.pathsep.join([*(str(path) for path in directories), os.environ.get("PATH", "")])
                # cuDNN also uses legacy LoadLibrary, which does not consistently
                # consult AddDllDirectory. Preload its runtime compiler by path.
                import ctypes
                for filename in ("cudart64_12.dll", "nvJitLink_120_0.dll", "nvrtc64_120_0.dll", "cublasLt64_12.dll", "cublas64_12.dll", "cufft64_11.dll", "curand64_10.dll", "cudnn64_9.dll"):
                    found = False
                    for directory in directories:
                        library = directory / filename
                        if library.exists():
                            if not library.resolve().is_relative_to(addon.resolve()):
                                raise RuntimeError(f"CUDA 动态库超出项目 core：{library}")
                            _DLL_HANDLES.append(ctypes.WinDLL(str(library)))
                            found = True
                            break
                    if not found:
                        raise RuntimeError(f"项目 core 内缺少 CUDA 动态库：{filename}")
                ort.preload_dlls(cuda=False, cudnn=True, msvc=False, directory=str(addon / "nvidia/cudnn/bin"))
            else:
                ort.preload_dlls(directory=str(addon / "nvidia/cudnn/lib"))
            ort._pdf2zh_cuda_ready = True
        except Exception as exc:
            LOG.warning("CUDA 动态库预加载失败，将检查会话是否可用：%s", exc)
    return ort


class FallbackSession:
    def __init__(self, ort, model):
        self.ort, self.serialized = ort, model
        self.session = None
        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" in providers and getattr(ort, "_pdf2zh_cuda_ready", True):
            options = {
                "device_id": int(os.environ.get("PDF2ZH_GPU_ID", "0")),
                "gpu_mem_limit": int(os.environ.get("PDF2ZH_GPU_MEMORY_MB", "2048")) * 1024 * 1024,
                "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "cudnn_conv_use_max_workspace": "0",
            }
            try:
                candidate = ort.InferenceSession(model, providers=[("CUDAExecutionProvider", options), "CPUExecutionProvider"])
                if "CUDAExecutionProvider" in candidate.get_providers():
                    self.session = candidate
                    LOG.info("版面识别实际后端：CUDA；GPU %s，显存上限 %s MB", options["device_id"], os.environ.get("PDF2ZH_GPU_MEMORY_MB", "2048"))
                else:
                    LOG.warning("CUDA 会话未启用，实际后端为 CPU；请检测 CUDA 组件和驱动")
            except Exception as exc:
                LOG.warning("CUDA 会话创建失败，回退 CPU：%s", exc)
        else:
            emit = LOG.warning if os.environ.get("PDF2ZH_DEVICE") == "cuda" else LOG.info
            emit("当前运行时没有 CUDAExecutionProvider，版面识别使用 CPU；可在内核页安装 CUDA 组件")
        if self.session is None:
            self.session = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
            LOG.info("版面识别实际后端：CPU")

    def run(self, *args, **kwargs):
        try:
            return self.session.run(*args, **kwargs)
        except Exception as exc:
            if "CUDAExecutionProvider" not in self.session.get_providers():
                raise
            LOG.warning("CUDA 推理失败（可能显存不足），回退 CPU：%s", exc)
            self.session = self.ort.InferenceSession(self.serialized, providers=["CPUExecutionProvider"])
            return self.session.run(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.session, name)


def configure_layout(ort):
    from babeldoc.docvision import doclayout
    if os.environ.get("PDF2ZH_DEVICE", "auto") == "cpu":
        LOG.info("版面识别已设置为 CPU 模式")
        return
    original = doclayout.OnnxModel.__init__

    def initialize(self, model_path):
        if sys.platform == "darwin":
            return original(self, model_path)
        self.model_path = model_path
        model = doclayout.onnx.load(model_path)
        metadata = {item.key: item.value for item in model.metadata_props}
        self._stride = ast.literal_eval(metadata["stride"])
        self._names = ast.literal_eval(metadata["names"])
        self.model = FallbackSession(ort, model.SerializeToString())
        self.lock = threading.Lock()

    doclayout.OnnxModel.__init__ = initialize


def logged_progress_handler(original, config):
    emitter = ProgressEmitter()

    def handle(event):
        if event.get("type") == "error":
            LOG.error("翻译事件失败：%s", event.get("error", event))
            emitter(event)
            return
        emitter(event)

    # Rendering an interactive Rich frame into a pipe floods desktop logs.
    return contextlib.nullcontext(), handle


def configure_progress():
    from pdf2zh_next import high_level
    original = high_level.create_progress_handler
    high_level.create_progress_handler = lambda config: logged_progress_handler(original, config)


def configure_reference_layout():
    from reference_layout import install_reference_layout
    install_reference_layout()
    from layout_integrity import install_layout_integrity
    install_layout_integrity()


def initialize_worker_runtime():
    """Windows spawn reimports this file without entering main()."""
    hide_descendant_windows()
    configure_layout(load_runtime())
    configure_reference_layout()


def _probe_layout_worker(model_path, results):
    try:
        import numpy as np
        import onnxruntime as ort
        from babeldoc.docvision.doclayout import OnnxModel
        original_session = ort.InferenceSession
        def profiled_session(*args, **kwargs):
            options = ort.SessionOptions()
            options.enable_profiling = True
            options.profile_file_prefix = str(cuda_paths(os.environ['ZOTERO_PDF2ZH_APP_HOME'])['temporary'] / ('layout-worker-' + str(os.getpid())))
            kwargs['sess_options'] = options
            return original_session(*args, **kwargs)
        ort.InferenceSession = profiled_session
        model = OnnxModel(model_path)
        model.predict(np.zeros((800, 600, 3), dtype=np.uint8))
        providers = model.model.get_providers()
        events = json.loads(Path(model.model.end_profiling()).read_text(encoding='utf-8'))
        cuda_events = sum(event.get('args', {}).get('provider') == 'CUDAExecutionProvider' for event in events)
        if 'CUDAExecutionProvider' in providers and not cuda_events:
            raise RuntimeError('版面模型未在 CUDA 上执行任何算子')
        ort.InferenceSession = original_session
        modules = loaded_cuda_modules() if os.name == 'nt' and 'CUDAExecutionProvider' in providers else []
        core = cuda_paths(os.environ['ZOTERO_PDF2ZH_APP_HOME'], create=False)['core']
        if any(not Path(path).resolve().is_relative_to(core) for path in modules):
            raise RuntimeError('翻译工作进程加载了项目 core 之外的 CUDA 动态库')
        from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
        from babeldoc.format.pdf.document_il.backend.pdf_creater import RenderContext
        results.put({'providers': providers, 'cuda_node_events': cuda_events, 'cuda_modules_in_core': len(modules),
                     'reference_layout_adapter': bool(getattr(Typesetting, '_pdf2zh_reference_adapter', False)),
                     'layout_integrity_adapter': bool(getattr(RenderContext, '_pdf2zh_layout_integrity', False))})
    except Exception as exc:
        results.put({'error': f'{type(exc).__name__}: {exc}'})


def probe_worker_layout(model_path):
    import multiprocessing
    import queue
    context = multiprocessing.get_context('spawn')
    results = context.Queue()
    process = context.Process(target=_probe_layout_worker, args=(model_path, results))
    process.start()
    try:
        try:
            result = results.get(timeout=90)
        except queue.Empty:
            raise RuntimeError('翻译工作进程版面识别检测超时') from None
        process.join(timeout=10)
        if process.exitcode != 0 or result.get('error'):
            raise RuntimeError(result.get('error') or f'工作进程退出码 {process.exitcode}')
        if os.environ.get('PDF2ZH_DEVICE') != 'cpu' and 'CUDAExecutionProvider' not in result['providers']:
            raise RuntimeError('翻译工作进程实际未使用 CUDA')
        LOG.info('翻译工作进程版面模型推理通过：%s', json.dumps(result, ensure_ascii=False))
    finally:
        if process.is_alive():
            process.terminate(); process.join(5)
        results.close(); results.join_thread()


def probe_cuda(ort):
    """Run and profile a real GPU operation; listing providers alone is insufficient."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper
    if "CUDAExecutionProvider" not in ort.get_available_providers() or not getattr(ort, "_pdf2zh_cuda_ready", False):
        raise RuntimeError("项目 core 内的 CUDA 运行库不可用；请先安装 CUDA 加速组件")
    nodes = [helper.make_node("MatMul", ["x", "y"], ["z"]), helper.make_node("Conv", ["image", "weights"], ["conv"])]
    inputs = [helper.make_tensor_value_info(name, TensorProto.FLOAT, [64, 64]) for name in ("x", "y")]
    inputs += [helper.make_tensor_value_info("image", TensorProto.FLOAT, [1, 3, 32, 32]), helper.make_tensor_value_info("weights", TensorProto.FLOAT, [8, 3, 3, 3])]
    outputs = [helper.make_tensor_value_info("z", TensorProto.FLOAT, [64, 64]), helper.make_tensor_value_info("conv", TensorProto.FLOAT, [1, 8, 30, 30])]
    graph = helper.make_graph(nodes, "cuda-check", inputs, outputs)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 10
    options = ort.SessionOptions()
    options.enable_profiling = True
    options.profile_file_prefix = str(cuda_paths(os.environ["ZOTERO_PDF2ZH_APP_HOME"])["temporary"] / "cuda-check")
    session = ort.InferenceSession(model.SerializeToString(), sess_options=options, providers=[("CUDAExecutionProvider", {"device_id": int(os.environ.get("PDF2ZH_GPU_ID", "0"))}), "CPUExecutionProvider"])
    if "CUDAExecutionProvider" not in session.get_providers():
        raise RuntimeError("CUDA 动态库或驱动加载失败，会话实际已回退 CPU；请查看前面的错误日志")
    feed = {name: np.ones((64, 64), np.float32) for name in ("x", "y")}
    feed.update({"image": np.ones((1, 3, 32, 32), np.float32), "weights": np.ones((8, 3, 3, 3), np.float32)})
    result = session.run(None, feed)
    np.testing.assert_allclose(result[0], 64.0)
    np.testing.assert_allclose(result[1], 27.0)
    profile = Path(session.end_profiling())
    events = json.loads(profile.read_text(encoding="utf-8"))
    operations = {event.get("args", {}).get("op_name") for event in events if event.get("args", {}).get("provider") == "CUDAExecutionProvider"}
    if not {"MatMul", "Conv"}.issubset(operations):
        raise RuntimeError("矩阵及卷积测试未全部在 CUDA 上执行")
    if os.name == "nt":
        paths = cuda_paths(os.environ["ZOTERO_PDF2ZH_APP_HOME"])
        modules = loaded_cuda_modules()
        outside = [path for path in modules if not Path(path).resolve().is_relative_to(paths["core"])]
        if outside:
            raise RuntimeError(f"检测到来自项目 core 以外的 CUDA 运行库：{outside}")
        if not modules:
            raise RuntimeError("没有找到可验证的项目 CUDA 动态库")
        (paths["temporary"] / "cuda-loaded-modules.json").write_text(json.dumps(modules, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("已核对 %s 个 CUDA 动态库，全部来自项目 core；未使用系统 CUDA Toolkit", len(modules))
    LOG.info("CUDA 检测通过：GPU %s 已实际执行矩阵及 cuDNN 卷积运算；ONNX Runtime %s", os.environ.get("PDF2ZH_GPU_ID", "0"), ort.__version__)


def main():
    hide_descendant_windows()
    os.environ['PDF2ZH_BOOTSTRAP_WORKER'] = '1'
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ort = load_runtime()
    if sys.argv[1:] == ["--probe-cuda"]:
        probe_cuda(ort)
        return
    configure_layout(ort)
    configure_reference_layout()
    if len(sys.argv) == 3 and sys.argv[1] == '--probe-worker-layout':
        probe_worker_layout(sys.argv[2])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-layout":
        import numpy as np
        from babeldoc.docvision.doclayout import OnnxModel
        model = OnnxModel(sys.argv[2])
        model.predict(np.zeros((800, 600, 3), dtype=np.uint8))
        LOG.info("版面模型推理通过；实际后端 %s", model.model.get_providers())
        if os.environ.get("PDF2ZH_DEVICE") != "cpu" and "CUDAExecutionProvider" not in model.model.get_providers():
            raise RuntimeError("版面模型的 CUDA 推理未通过；已验证 CPU 回退可用")
        return
    configure_progress()
    from pdf2zh_next.main import cli
    sys.argv[0] = "pdf2zh_next"
    cli()


if __name__ == "__main__":
    main()
elif __name__ == '__mp_main__' and os.environ.get('PDF2ZH_BOOTSTRAP_WORKER') == '1':
    initialize_worker_runtime()
