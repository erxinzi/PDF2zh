import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.app_logging import AppLogger, AppLogHandler, LogStream, infer_level, log_source
from utils.process_utils import stream_process


class DesktopLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logger = AppLogger(Path(self.temp.name) / "app.log")

    def tearDown(self):
        self.temp.cleanup()

    def test_levels_are_based_on_content_not_stderr(self):
        for message, expected in (
            ("INFO loading document", "INFO"),
            ("WARNING cannot load CUDA, using CPU", "WARNING"),
            ("UserWarning: optional feature", "WARNING"),
            ("ImportError: cannot import name 'BitStream'", "ERROR"),
            ("\x1b[31mERROR failed\x1b[0m", "ERROR"),
            ("[E:onnxruntime:Default] DLL load failed", "ERROR"),
            ("[W:onnxruntime:Default] CPU fallback", "WARNING"),
            ("Translate 1/10", "INFO"),
        ):
            self.assertEqual(infer_level(message), expected, message)

    def test_stream_buffers_fragments_and_marks_the_full_traceback(self):
        stream = LogStream(self.logger)
        stream.write("loading ")
        stream.write("document\nTraceback (most recent call last):\n")
        stream.write('  File "kernel.py", line 1\n')
        stream.write("ImportError: BitStream missing\nINFO recovered\n")
        entries = self.logger.snapshot()
        self.assertEqual([item.level for item in entries], ["INFO", "ERROR", "ERROR", "ERROR", "INFO"])
        self.assertEqual(entries[0].message, "loading document")

    def test_thread_fragments_and_sources_do_not_mix(self):
        stream = LogStream(self.logger)
        barrier = threading.Barrier(2)

        def emit(source, word):
            with log_source(source):
                stream.write(word)
                barrier.wait()
                stream.write(" complete\n")

        workers = [threading.Thread(target=emit, args=args) for args in (("内核", "download"), ("服务", "translate"))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual({(item.source, item.message) for item in self.logger.snapshot()}, {("内核", "download complete"), ("服务", "translate complete")})

    def test_restart_restores_levels_and_legacy_log_lines(self):
        self.logger.path.write_text("[19:29:25] [内核] ImportError: old failure\n", encoding="utf-8")
        self.logger.log("服务", "new warning", "WARNING")
        restored = AppLogger(self.logger.path)
        self.assertEqual([item.level for item in restored.snapshot()], ["ERROR", "WARNING"])
        self.assertTrue(restored.ui_queue.empty())

    def test_bounded_log_queue_keeps_recent_entries(self):
        logger = AppLogger(self.logger.path, max_entries=2)
        for value in range(4):
            logger.log("系统", str(value))
        self.assertEqual([item.message for item in logger.snapshot()], ["2", "3"])
        self.assertEqual(logger.ui_queue.qsize(), 2)

    def test_hidden_child_errors_reach_log_progress_and_http_payload(self):
        from flask import Flask
        from server import PDFTranslator
        kernel = logging.getLogger("pdf2zh.kernel")
        handler = AppLogHandler(self.logger)
        previous_level, previous_propagate = kernel.level, kernel.propagate
        kernel.setLevel(logging.INFO)
        kernel.propagate = False
        kernel.addHandler(handler)
        progress = []
        command = [sys.executable, "-u", "-c", "import sys; print('INFO initialize'); print('WARNING using CPU', file=sys.stderr); print('Translate 1/3'); raise ImportError('cannot import name BitStream')"]
        original = subprocess.Popen
        try:
            with patch("utils.process_utils.subprocess.Popen", wraps=original) as spawn:
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    stream_process(command, on_line=progress.append, task_id="test-task")
            if os.name == "nt":
                self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertIn("Translate 1/3", progress)
            translator = PDFTranslator.__new__(PDFTranslator)
            translator.app = Flask("desktop-error-test")
            translator.app.logger.handlers = [handler]
            translator.app.logger.propagate = False
            payload = translator._exception_payload(caught.exception, context="paper.pdf")
            self.assertEqual(payload["errorType"], "ImportError")
            self.assertEqual(payload["message"], "ImportError: cannot import name BitStream")
            self.assertEqual(payload["exitCode"], 1)
            entries = self.logger.snapshot()
            self.assertTrue(any(item.level == "WARNING" and "using CPU" in item.message for item in entries))
            self.assertTrue(any(item.level == "ERROR" and "BitStream" in item.message for item in entries))
            persisted = self.logger.path.read_text(encoding="utf-8")
            self.assertIn("[ERROR]", persisted)
            self.assertIn("Traceback", persisted)
        finally:
            kernel.removeHandler(handler)
            kernel.setLevel(previous_level)
            kernel.propagate = previous_propagate

    def test_timeout_is_logged_and_does_not_hang_on_a_silent_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            stream_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.2)

    @unittest.skipUnless(os.name == "nt", "Windows process creation")
    def test_multiprocessing_children_are_hidden_too(self):
        import _winapi
        from runtime_bootstrap import hide_descendant_windows
        with patch.object(subprocess, "Popen", subprocess.Popen), patch.object(_winapi, "CreateProcess") as create:
            hide_descendant_windows()
            _winapi.CreateProcess("python", "python child.py", None, None, False, 0, None, None, None)
            self.assertTrue(create.call_args.args[5] & subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
