import json
import os
import threading
import time
import math
import logging
from pathlib import Path
from utils.task_control import TaskControl, TaskCancelled
from datetime import datetime

_CURRENT_TASK = threading.local()


def checkpoint_current_task():
    current = getattr(_CURRENT_TASK, "value", None)
    if current:
        current[0].checkpoint(current[1])


# DEBUG_PROGRESS_LOG_PATH = os.path.join(
#     os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
#     "_debug_progress.log",
# )
# _DEBUG_LOG_LOCK = threading.Lock()


# def _debug_progress_log(stage, **fields):
#     try:
#         ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
#         parts = []
#         for key in sorted(fields.keys()):
#             value = str(fields[key]).replace("\r", "\\r").replace("\n", "\\n")
#             if len(value) > 800:
#                 value = value[:800] + "..."
#             parts.append(f"{key}={value}")
#         line = f"[{ts}] [{stage}] " + " ".join(parts)
#         with _DEBUG_LOG_LOCK:
#             with open(DEBUG_PROGRESS_LOG_PATH, "a", encoding="utf-8", errors="replace") as fp:
#                 fp.write(line + "\n")
#     except Exception:
#         pass


class TaskManager:
    def __init__(self):
        self.active_tasks = {}
        self.lock = threading.Lock()
        self.progress_history = []
        self.revision = 0
        self.controls = {}
        self.history_path = None
        self._history_write_lock = threading.Lock()
        self._execution_lock = threading.Lock()

    def configure_history(self, path):
        with self._history_write_lock:
            with self.lock:
                if self.history_path is not None:
                    return
                self.history_path = Path(path)
                try:
                    loaded = json.loads(self.history_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        self.progress_history = [item for item in loaded if isinstance(item, dict)][:200]
                        self.revision += 1
                except FileNotFoundError:
                    pass
                except (ValueError, OSError):
                    logging.getLogger(__name__).warning("无法读取历史任务记录", exc_info=True)

    def _save_history(self):
        if self.history_path is None:
            return
        with self._history_write_lock:
            with self.lock:
                history = list(self.progress_history)
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.history_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.history_path)
            except OSError:
                logging.getLogger(__name__).error("无法保存历史任务记录", exc_info=True)

    def control(self, task_id):
        with self.lock:
            return self.controls.get(task_id)

    def checkpoint(self, task_id):
        control = self.control(task_id)
        if control:
            control.checkpoint()

    def run_job(self, task_id, worker):
        # One document at a time avoids concurrent GPU model initialization and
        # output/config collisions. Paused queued tasks do not hold the slot.
        while True:
            self.checkpoint(task_id)
            if self._execution_lock.acquire(timeout=0.1):
                control = self.control(task_id)
                if control and control.paused:
                    self._execution_lock.release()
                    continue
                break
        try:
            self.checkpoint(task_id)
            self.update_task(task_id, {"status": "running"})
            previous = getattr(_CURRENT_TASK, "value", None)
            _CURRENT_TASK.value = (self, task_id)
            return worker()
        finally:
            _CURRENT_TASK.value = locals().get("previous")
            self._execution_lock.release()

    def act(self, task_id, action):
        control = self.control(task_id)
        if control is None:
            raise ValueError("任务不存在或已结束")
        with control.lock:
            with self.lock:
                task = self.active_tasks.get(task_id)
                if not task or task.get("finished"):
                    raise ValueError("任务已结束")
            if action == "pause":
                control.pause()
            elif action == "resume":
                control.resume()
            elif action == "cancel":
                control.cancel()
            else:
                raise ValueError("未知任务操作")
            if action == "cancel":
                self.complete_task(task_id, "cancelled", "用户取消任务")
            else:
                with self.lock:
                    now = time.monotonic()
                    if control.paused and "_pause_started" not in task:
                        task["_pause_started"] = now
                    elif not control.paused:
                        paused_at = task.pop("_pause_started", None)
                        if paused_at is not None and "_stage_started" in task:
                            task["_stage_started"] += now - paused_at
                    task["paused"] = control.paused
                    task["status"] = "paused" if control.paused else ("running" if task.get("stage") else "queued")
                    self.revision += 1
        logging.getLogger(__name__).info("任务 %s：%s", task_id, {"pause": "已暂停", "resume": "已继续", "cancel": "已取消"}[action])
        return True

    @staticmethod
    def _task_snapshot(task_id, task):
        snapshot = {
            "taskId": task_id,
            "fileName": task.get("fileName"),
            "active": task.get("active"),
            "progress": task.get("progress"),
            "status": task.get("status"),
            "message": task.get("message"),
            "engine": task.get("engine"),
            "service": task.get("service"),
        }
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    def add_task(self, task_id, info):
        with self.lock:
            self.active_tasks[task_id] = info
            self.controls[task_id] = TaskControl()
            self.revision += 1
            # _debug_progress_log("TASK_ADD", task=self._task_snapshot(task_id, self.active_tasks[task_id]))

    def update_task(self, task_id, updates):
        with self.lock:
            if task_id in self.active_tasks:
                task = self.active_tasks[task_id]
                if task.get("finished") or task.get("active") is False:
                    return
                updates = dict(updates)
                if task.get("paused"):
                    return
                stage = updates.get("stage")
                if stage and stage != task.get("stage"):
                    self._finish_stage(task)
                    task["_stage_started"] = time.monotonic()
                if "progress" in updates:
                    try:
                        progress = float(updates["progress"])
                        if not math.isfinite(progress):
                            return
                        updates["progress"] = max(float(task.get("progress", 0)), min(99, max(0, progress)))
                    except (ValueError, TypeError):
                        return
                if any(task.get(key) != value for key, value in updates.items()):
                    task.update(updates)
                    self.revision += 1
                # _debug_progress_log(
                #     "TASK_UPDATE",
                #     updates=json.dumps(updates, ensure_ascii=False, sort_keys=True),
                #     task=self._task_snapshot(task_id, self.active_tasks[task_id]),
                # )

    def complete_task(self, task_id, status, message=None, file_list=None, error=None, file_paths=None, output_dir=None, result=None):
        with self.lock:
            if task_id not in self.active_tasks:
                return
            task = self.active_tasks[task_id]
            # Client disconnect after a successful translate can raise while
            # returning HTTP; do not overwrite a completed success as failure.
            if task.get("finished"):
                return
            self._finish_stage(task)
            task["active"] = False
            task["finished"] = True
            task["status"] = {"success": "完成", "cancelled": "已取消"}.get(status, "失败")
            task["progress"] = 100 if status == "success" else task.get("progress", 0)
            if message:
                task["message"] = message
            task["endTime"] = datetime.now().isoformat()
            if file_list:
                task["fileList"] = list(file_list)
            if file_paths:
                task["filePaths"] = list(file_paths)
            if output_dir:
                task["outputDir"] = output_dir
            if result:
                task["result"] = result
            if error:
                task["error"] = str(error)

            history_item = {
                "taskId": task_id,
                "fileName": task.get("fileName"),
                "status": status if status in ("success", "cancelled") else "failed",
                "finished": True,
                "active": False,
                "progress": task["progress"],
                "engine": task.get("engine"),
                "service": task.get("service"),
                "startTime": task.get("startTime"),
                "endTime": task.get("endTime"),
                "config": task.get("config"),
                "stageSeconds": dict(task.get("stageSeconds", {})),
                "source": task.get("source", "plugin"),
                "message": task.get("message", ""),
                "qualityWarnings": list(task.get("qualityWarnings", [])),
                "translationErrorCount": task.get("translationErrorCount", 0),
            }
            if file_list:
                history_item["fileList"] = list(file_list)
            if file_paths:
                history_item["filePaths"] = list(file_paths)
            if output_dir:
                history_item["outputDir"] = output_dir
            if result:
                history_item["result"] = result
            if error:
                history_item["error"] = str(error)

            self.progress_history.insert(0, history_item)
            self.revision += 1
            if len(self.progress_history) > 200:
                self.progress_history = self.progress_history[:200]

            threading.Thread(target=self._delayed_remove, args=(task_id,), daemon=True).start()
        self._save_history()

    @staticmethod
    def _finish_stage(task):
        started = task.pop("_stage_started", None)
        if started is not None and task.get("stage"):
            stage = task["stage"]
            seconds = round(max(0, task.get("_pause_started", time.monotonic()) - started), 2)
            timings = task.setdefault("stageSeconds", {})
            timings[stage] = round(timings.get(stage, 0) + seconds, 2)
            logging.getLogger(__name__).info("任务 %s：阶段 %s 耗时 %.2f 秒", task.get("taskId"), stage, seconds)

    def get_active_tasks_list(self):
        with self.lock:
            tasks = [{key: value for key, value in task.items() if not key.startswith("_")} for task in self.active_tasks.values()]
            return tasks

    def get_history(self):
        with self.lock:
            return list(self.progress_history)

    def desktop_snapshot(self, after_revision=-1):
        # The GUI must never wait behind a task update or large HTTP response.
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self.revision == after_revision:
                return None
            keys = ("taskId", "fileName", "status", "active", "progress", "engine", "service", "message", "paused", "filePaths", "startTime", "endTime", "stage", "stageSeconds", "source", "error")
            compact = lambda task: {key: task.get(key) for key in keys if key in task}
            return self.revision, [compact(task) for task in list(self.active_tasks.values())[:200]], [compact(task) for task in self.progress_history[:200]]
        finally:
            self.lock.release()

    def _delayed_remove(self, task_id):
        time.sleep(30)
        with self.lock:
            if task_id in self.active_tasks:
                # _debug_progress_log("TASK_REMOVE", task_id=task_id)
                del self.active_tasks[task_id]
                self.controls.pop(task_id, None)
                self.revision += 1


# global singleton
task_manager = TaskManager()
