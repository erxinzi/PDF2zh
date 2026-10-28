import threading
import time
import unittest

from utils.ui_jobs import UiJobs, task_rows


class UiJobsTests(unittest.TestCase):
    def test_slow_work_never_blocks_submit_and_callbacks_run_on_the_caller_thread(self):
        jobs = UiJobs()
        release = threading.Event()
        finished = threading.Event()
        callbacks = []
        worker_ids = []

        def work():
            worker_ids.append(threading.get_ident())
            release.wait(2)
            finished.set()
            return "ready"

        begin = time.perf_counter()
        self.assertTrue(jobs.submit("service", work, lambda value: callbacks.append((threading.get_ident(), value))))
        self.assertLess(time.perf_counter() - begin, 0.1)
        self.assertFalse(jobs.submit("service", work, callbacks.append))
        self.assertEqual(jobs.drain(), 0)
        release.set()
        self.assertTrue(finished.wait(2))
        # The result is queued before drain; wait without touching any Tk object.
        result = jobs.results.get(timeout=2)
        jobs.results.put(result)
        jobs.drain()
        self.assertEqual(callbacks, [(threading.get_ident(), "ready")])
        self.assertNotEqual(worker_ids[0], threading.get_ident())

    def test_closing_the_window_discards_late_worker_results(self):
        jobs = UiJobs()
        release = threading.Event()
        callbacks = []
        jobs.submit("worker", lambda: release.wait(1), callbacks.append)
        jobs.close()
        release.set()
        jobs.drain()
        self.assertEqual(callbacks, [])
        self.assertFalse(jobs.submit("later", lambda: None, callbacks.append))

    def test_completed_tasks_are_not_duplicated_in_active_and_history_rows(self):
        completed = {"taskId": "job", "fileName": "paper.pdf", "status": "完成", "progress": 100}
        history = {"taskId": "job", "fileName": "paper.pdf", "status": "success"}
        rows = task_rows([completed], [history])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1][2], "100%")


if __name__ == "__main__":
    unittest.main()
