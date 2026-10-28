import io
import json
import logging
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from runtime_progress import PREFIX, ProgressEmitter, decode_progress
from utils import execute
from utils.process_utils import stream_process
from utils.task_manager import TaskManager


class ProgressProtocolTests(unittest.TestCase):
    def test_progress_is_not_inferred_from_translated_source_or_truncated_rich_rows(self):
        manager = TaskManager()
        manager.add_task("job", {"active": True, "progress": 0})
        with patch.object(execute, "task_manager", manager):
            for line in ("translate 100/100 in a JavaScript example", "Translate Paragraphs (1/1) ---- 10/…", "translate --- 75/…"):
                execute._parse_progress(line, "job", structured_only=True)
        self.assertEqual(manager.active_tasks["job"]["progress"], 0)

    def test_emitter_preserves_real_stage_data_and_coalesces_repeated_updates(self):
        output = io.StringIO()
        emitter = ProgressEmitter(output, clock=lambda: 10.0)
        for index in range(1000):
            emitter({"type": "progress_update", "overall_progress": 25.2, "stage": "Translate Paragraphs", "stage_current": index, "stage_total": 1000})
        events = [decode_progress(line) for line in output.getvalue().splitlines() if line.startswith(PREFIX)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["progress"], 25.2)
        self.assertEqual(events[0]["stage"], "Translate Paragraphs")

    def test_invalid_or_nonfinite_progress_is_rejected(self):
        for value in (True, -1, 101, "NaN", "Infinity", None):
            self.assertIsNone(decode_progress(PREFIX + json.dumps({"version": 1, "progress": value})))

    def test_live_child_progress_reaches_task_manager_before_process_exits(self):
        manager = TaskManager()
        manager.add_task("job", {"active": True, "progress": 0})
        reached = threading.Event()
        errors = []
        line = PREFIX + json.dumps({"version": 1, "progress": 47, "stage": "Translate Paragraphs"})
        command = [sys.executable, "-u", "-c", f"import time; print({line!r}, flush=True); time.sleep(0.6)"]

        def parse(text):
            execute._parse_progress(text, "job", structured_only=True)
            if manager.active_tasks["job"]["progress"] == 47:
                reached.set()

        def run():
            try:
                stream_process(command, on_line=parse)
            except Exception as exc:
                errors.append(exc)

        with patch.object(execute, "task_manager", manager):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(reached.wait(5))
            self.assertTrue(worker.is_alive(), "progress must be visible while the child is running")
            worker.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(manager.active_tasks["job"]["progress"], 47)

    def test_progress_is_monotonic_and_completion_cannot_be_overwritten(self):
        manager = TaskManager()
        manager.add_task("job", {"active": True, "progress": 0})
        for percent in (40, 10, 100):
            manager.update_task("job", {"progress": percent})
        self.assertEqual(manager.active_tasks["job"]["progress"], 99)
        manager.complete_task("job", "success")
        manager.update_task("job", {"progress": 1, "status": "running"})
        self.assertEqual(manager.active_tasks["job"]["progress"], 100)
        self.assertEqual(manager.active_tasks["job"]["status"], "完成")

    def test_desktop_snapshot_never_waits_for_a_busy_task_lock(self):
        manager = TaskManager()
        manager.add_task("job", {"active": True, "progress": 0})
        revision, _, _ = manager.desktop_snapshot()
        self.assertIsNone(manager.desktop_snapshot(revision))
        manager.lock.acquire()
        try:
            begin = time.perf_counter()
            self.assertIsNone(manager.desktop_snapshot())
            self.assertLess(time.perf_counter() - begin, 0.05)
        finally:
            manager.lock.release()


if __name__ == "__main__":
    unittest.main()
