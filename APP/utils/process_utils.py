"""Keep desktop child processes hidden and pipe their complete output to logs."""
from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import sys
import threading
import contextlib

from utils.app_logging import clean_output, infer_level
from runtime_progress import decode_progress


def desktop_mode():
    return bool(getattr(sys, "frozen", False) or os.environ.get("ZOTERO_PDF2ZH_DESKTOP") == "1")


def hidden_process_kwargs():
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def run_process(*args, **kwargs):
    for key, value in hidden_process_kwargs().items():
        kwargs.setdefault(key, value)
    return subprocess.run(*args, **kwargs)


class KernelTranslationError(RuntimeError):
    """A fatal kernel event is authoritative even when its CLI exits with zero."""
    def __init__(self, kind, message, output, returncode):
        self.kernel_error_type = kind
        self.kernel_exit_code = returncode
        self.output = self.stderr = output
        super().__init__(f'{kind}: {message}')


def stream_process(command, *, env=None, cwd=None, timeout=None, on_line=None, task_id=None):
    logger = logging.getLogger("pdf2zh.kernel")
    prefix = f"[任务 {task_id}] " if task_id else ""
    tail = collections.deque()
    size = 0
    trace = False
    kernel_error = None
    timed_out = threading.Event()
    from utils.task_manager import task_manager
    from utils.translation_quality import TranslationQuality
    quality = TranslationQuality(task_id, task_manager)
    task_manager.checkpoint(task_id)
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, cwd=cwd,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        **hidden_process_kwargs(),
    )

    def expire():
        timed_out.set()
        try:
            process.kill()
        except OSError:
            pass

    timer = threading.Timer(timeout, expire) if timeout else None
    if timer:
        timer.daemon = True
        timer.start()
    control = task_manager.control(task_id)
    try:
      with control.bind(process) if control else contextlib.nullcontext():
        for raw in process.stdout:
            line = clean_output(raw).rstrip()
            if not line:
                continue
            if on_line:
                on_line(line)
            quality.feed(line)
            event = decode_progress(line)
            if event is not None:
                if event.get('type') == 'error':
                    kernel_error = event
                    line = f"{event.get('errorType', 'KernelTranslationError')}: {event.get('error', '翻译内核报告失败')}"
                else:
                    line = f"进度 {event['progress']:g}% · {event['stage']} ({event.get('current', 0)}/{event.get('total', 0)})"
            if "Traceback (most recent call last)" in line:
                trace = True
            level = "ERROR" if trace or (event is not None and event.get('type') == 'error') else infer_level(line)
            logger.log(getattr(logging, level), prefix + line)
            if re.search(r"\w*(?:Error|Exception):", line):
                trace = False
            tail.append(line + "\n")
            size += len(line) + 1
            while size > 60000 and len(tail) > 1:
                size -= len(tail.popleft())
        return_code = process.wait()
    finally:
        if timer:
            timer.cancel()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    output = "".join(tail)
    if timed_out.is_set():
        logger.error("%s子进程超时（%s 秒）", prefix, timeout)
        raise subprocess.TimeoutExpired(command, timeout, output=output, stderr=output)
    if kernel_error is not None:
        raise KernelTranslationError(kernel_error.get('errorType', 'KernelTranslationError'),
                                     kernel_error.get('error', '翻译内核报告失败'), output, return_code)
    if return_code:
        logger.error("%s子进程退出，退出码 %s", prefix, return_code)
        raise subprocess.CalledProcessError(return_code, command, output=output, stderr=output)
    return subprocess.CompletedProcess(command, return_code, stdout=output, stderr="")
