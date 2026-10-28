"""Background jobs deliver results through a queue; workers never call Tk."""
from __future__ import annotations

import logging
import queue
import threading
import time


class UiJobs:
    def __init__(self):
        self.results = queue.Queue()
        self._active = set()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, key, work, apply, on_error=None):
        with self._lock:
            if self._closed or key in self._active:
                return False
            self._active.add(key)

        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
                logging.getLogger(__name__).exception("后台操作失败：%s", key)
            with self._lock:
                if self._closed:
                    self._active.discard(key)
                    return
                self.results.put((key, apply, on_error, value, error))

        threading.Thread(target=run, name=f"pdf2zh-{key}", daemon=True).start()
        return True

    def drain(self, max_items=10, time_budget=0.008):
        deadline = time.perf_counter() + time_budget
        count = 0
        while count < max_items and time.perf_counter() < deadline:
            try:
                key, apply, on_error, value, error = self.results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._active.discard(key)
                closed = self._closed
            if not closed:
                try:
                    if error is None:
                        apply(value)
                    elif on_error:
                        on_error(error)
                except Exception:
                    logging.getLogger(__name__).exception("界面结果处理失败：%s", key)
            count += 1
        return count

    def close(self):
        with self._lock:
            self._closed = True

    def busy(self, key=None):
        with self._lock:
            return bool(self._active) if key is None else key in self._active


def task_rows(tasks, history):
    """Stable row identifiers avoid rebuilding the task table every refresh."""
    rows, seen = [], set()
    for index, task in enumerate((tasks or [])[:200] + (history or [])[:200]):
        key = str(task.get("taskId") or f"legacy-{index}-{task.get('fileName', '')}")
        if key in seen:
            continue
        seen.add(key)
        status = task.get("status") or ("进行中" if task.get("active") else "-")
        if status in ("success", "完成"):
            status, percent = "完成（有警告）" if task.get('qualityWarnings') else "完成", 100
        elif status in ("failed", "失败"):
            status, percent = "失败", task.get("progress", 0)
        elif status in ("cancelled", "已取消"):
            status, percent = "已取消", task.get("progress", 0)
        elif status == "paused" or task.get("paused"):
            status, percent = "已暂停", task.get("progress", 0)
        elif status == "queued":
            status, percent = "等待中", task.get("progress", 0)
        else:
            status, percent = "进行中" if task.get("active") else str(status), task.get("progress", 0)
        try:
            progress = f"{max(0, min(100, int(float(percent))))}%"
        except (TypeError, ValueError, OverflowError):
            progress = "0%"
        values = (str(task.get("fileName") or key), status, progress, str(task.get("engine") or task.get("service") or "-"))
        rows.append((key, values))
    return rows
