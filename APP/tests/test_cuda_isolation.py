import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_isolation import cuda_environment, cuda_paths, require_project_python
from utils.gpu_runtime import install_cuda, runtime_env


class CudaIsolationTests(unittest.TestCase):
    def test_gpu_caches_and_temporary_paths_are_under_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = {"UV_CACHE_DIR": "outside-uv-cache", "UV_TARGET": "outside-target", "UV_CONFIG_FILE": "global-uv.toml",
                      "PIP_TARGET": "outside-site-packages", "PYTHONPATH": "global-python", "VIRTUAL_ENV": "global-env",
                      "CUDA_PATH": "system-cuda", "CUDA_CACHE_PATH": "global-cache", "HTTPS_PROXY": "http://proxy"}
            expected_source = dict(source)
            env = cuda_environment(root, source)
            for key in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "PIP_CACHE_DIR", "TEMP", "TMP", "TMPDIR", "CUDA_CACHE_PATH", "CUDA_PATH", "CUDA_HOME", "CUDNN_PATH", "PYTHONUSERBASE"):
                self.assertTrue(Path(env[key]).resolve().is_relative_to(root / "core"), key)
            self.assertNotIn("UV_TARGET", env)
            self.assertNotIn("UV_CONFIG_FILE", env)
            self.assertNotIn("PIP_TARGET", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("VIRTUAL_ENV", env)
            self.assertEqual(env["UV_PYTHON_DOWNLOADS"], "never")
            self.assertEqual(env["HTTPS_PROXY"], source["HTTPS_PROXY"])
            self.assertEqual(source, expected_source)

    def test_installer_uses_explicit_project_python_target_and_bundled_uv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "core/venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            uv = root / "_tools/uv.exe"
            uv.parent.mkdir()
            uv.touch()
            original = dict(os.environ)
            with patch("app_paths.resource_path", return_value=uv), patch("utils.gpu_runtime.stream_process") as execute, patch("utils.gpu_runtime.probe_cuda"):
                target = install_cuda(root, python)
            command = execute.call_args.args[0]
            self.assertEqual(command[0], str(uv))
            self.assertEqual(Path(command[command.index("--target") + 1]), root / "core/onnxruntime-cuda")
            self.assertEqual(Path(command[command.index("--python") + 1]), python)
            self.assertIn("--no-config", command)
            self.assertIn("--no-python-downloads", command)
            self.assertIn("copy", command)
            self.assertNotIn("--system", command)
            self.assertTrue((target / "ready.json").is_file())
            self.assertEqual(dict(os.environ), original)

    def test_global_python_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "system-python.exe"
            python.touch()
            with patch("utils.gpu_runtime.stream_process") as execute:
                with self.assertRaises(ValueError):
                    install_cuda(root, python)
            execute.assert_not_called()

    def test_directory_junction_cannot_redirect_cuda_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            core = project / "core"
            core.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            link = core / "onnxruntime-cuda"
            try:
                if os.name == "nt":
                    import _winapi
                    _winapi.CreateJunction(str(outside), str(link))
                else:
                    link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    cuda_paths(project)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if link.exists():
                    link.rmdir() if os.name == "nt" else link.unlink()

    def test_cuda_probe_profile_and_runtime_cache_remain_in_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = runtime_env(root, {"USERPROFILE": "real-user", "APPDATA": "real-appdata"}, probe=True)
            if os.name == "nt":
                for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                    self.assertTrue(Path(env[key]).is_relative_to(root / "core"))
            self.assertTrue(Path(env["CUDA_CACHE_PATH"]).is_relative_to(root / "core"))
            self.assertFalse((root / "cache").exists())


if __name__ == "__main__":
    unittest.main()
