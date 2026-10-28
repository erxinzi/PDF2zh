import json
import io
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from runtime_bootstrap import FallbackSession, logged_progress_handler
from utils.gpu_runtime import load_gpu_settings, prepare_runtime_command, runtime_env
from utils.process_utils import run_process


class CudaRuntimeTests(unittest.TestCase):
    def test_progress_events_still_update_desktop_without_a_console(self):
        from utils import execute
        original = Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            returned, handle = logged_progress_handler(original, None)
            handle({"type": "progress_update", "overall_progress": 33, "stage": "Translate", "stage_current": 1, "stage_total": 3})
        with patch.object(execute, "task_manager") as manager:
            execute._parse_progress(output.getvalue(), "progress-test", structured_only=True)
        self.assertEqual(manager.update_task.call_args.args[1]["progress"], 33)
        original.assert_not_called()

    def test_broken_gpu_addon_does_not_prevent_cpu_cli_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            addon = root / "core/onnxruntime-cuda"
            package = addon / "onnxruntime"
            package.mkdir(parents=True)
            (addon / "ready.json").write_text("{}", encoding="utf-8")
            (package / "__init__.py").write_text("raise ImportError('simulated missing GPU DLL')", encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "runtime_bootstrap.py"
            result = run_process([sys.executable, "-u", str(script), "--help"], env=runtime_env(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            self.assertIn("simulated missing GPU DLL", result.stderr)
            self.assertIn("--deepseek-thinking-mode", result.stdout)

    def test_gpu_session_failure_falls_back_to_cpu(self):
        ort = Mock()
        ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        cpu = Mock()
        ort.InferenceSession.side_effect = [RuntimeError("CUDA DLL missing"), cpu]
        session = FallbackSession(ort, b"onnx model")
        self.assertIs(session.session, cpu)
        self.assertEqual(ort.InferenceSession.call_args.kwargs["providers"], ["CPUExecutionProvider"])

    def test_gpu_inference_out_of_memory_retries_on_cpu(self):
        ort = Mock()
        ort.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        gpu, cpu = Mock(), Mock()
        gpu.get_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        gpu.run.side_effect = RuntimeError("CUDA out of memory")
        cpu.run.return_value = ["result"]
        ort.InferenceSession.side_effect = [gpu, cpu]
        session = FallbackSession(ort, b"model")
        self.assertEqual(session.run(None, {"images": "input"}), ["result"])
        self.assertIs(session.session, cpu)

    def test_cpu_inference_errors_are_not_hidden(self):
        ort = Mock()
        ort.get_available_providers.return_value = ["CPUExecutionProvider"]
        cpu = ort.InferenceSession.return_value
        cpu.get_providers.return_value = ["CPUExecutionProvider"]
        cpu.run.side_effect = RuntimeError("invalid model")
        with self.assertRaisesRegex(RuntimeError, "invalid model"):
            FallbackSession(ort, b"model").run(None, {})

    def test_settings_are_scoped_to_child_and_reject_invalid_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(json.dumps({"device": "cpu", "gpu_id": 1, "gpu_memory_mb": 1024}), encoding="utf-8")
            source = {"USERPROFILE": "real-profile", "MY_API_SETTING": "keep"}
            env = runtime_env(root, source)
            self.assertEqual(env["PDF2ZH_DEVICE"], "cpu")
            self.assertEqual(env["PDF2ZH_GPU_MEMORY_MB"], "1024")
            self.assertEqual(source["USERPROFILE"], "real-profile")
            self.assertEqual(env["MY_API_SETTING"], "keep")
            self.assertEqual(runtime_env(root, source, probe=True)["PDF2ZH_DEVICE"], "cuda")
            (root / "settings.json").write_text('{"gpu_memory_mb": 0}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_gpu_settings(root)

    def test_runtime_adapter_preserves_translation_and_deepseek_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / ("python.exe" if os.name == "nt" else "python")
            python.touch()
            command = [str(root / "pdf2zh_next.exe"), "paper.pdf", "--deepseek", "--deepseek-thinking-mode", "disabled"]
            result, env = prepare_runtime_command(command, {"ZOTERO_PDF2ZH_APP_HOME": str(root)})
            self.assertEqual(result[0], str(python))
            self.assertEqual(result[3:], command[1:])
            self.assertEqual(Path(result[2]).name, "runtime_bootstrap.py")
            self.assertEqual(env["PDF2ZH_GPU_ID"], "0")


if __name__ == "__main__":
    unittest.main()
