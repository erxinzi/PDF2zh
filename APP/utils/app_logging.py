"""Desktop logging shared by Python output, logging records and child processes."""
from __future__ import annotations

import contextlib
import contextvars
import io
import logging
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple


LEVELS = ("INFO", "WARNING", "ERROR")
_SOURCE = contextvars.ContextVar("desktop_log_source", default="服务")
_ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_LEVEL = re.compile(r"(?:^|[\s\[:])(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)(?=[\s\]:]|$)", re.I)
_EXCEPTION = re.compile(r"(?:\w*(?:Error|Exception)|KeyboardInterrupt|SystemExit):")
_SAVED = re.compile(r"^\[([^]]+)\] \[([^]]+)\] (?:\[(INFO|WARNING|ERROR)\] )?(.*)$")


def clean_output(text: str) -> str:
    # Some native Windows libraries mix UTF-16 ASCII with Python's UTF-8 pipe.
    return _ANSI.sub("", str(text).replace("\x00", ""))


def infer_level(text: str, default: str = "INFO") -> str:
    text = clean_output(text)
    match = _LEVEL.search(text)
    if match:
        level = match.group("level").upper()
        return {"WARN": "WARNING", "CRITICAL": "ERROR", "DEBUG": "INFO"}.get(level, level)
    if "Traceback (most recent call last)" in text or _EXCEPTION.search(text):
        return "ERROR"
    if any(mark in text for mark in ("❌", "翻译失败", "安装失败", "启动失败", "异常:")):
        return "ERROR"
    if "⚠" in text or re.search(r"\b\w*Warning:", text):
        return "WARNING"
    if re.search(r"\[E:onnxruntime", text):
        return "ERROR"
    if re.search(r"\[W:onnxruntime", text):
        return "WARNING"
    return default


class LogEntry(NamedTuple):
    timestamp: str
    source: str
    level: str
    message: str


class AppLogger:
    def __init__(self, path: Path, max_entries: int = 5000):
        self.path = Path(path)
        self.max_entries = max_entries
        self.entries: list[LogEntry] = []
        self.ui_queue: queue.Queue[LogEntry] = queue.Queue(maxsize=max_entries)
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._listeners = []
        self._load_recent()

    def _load_recent(self):
        for path in (self.path.with_name(self.path.name + ".1"), self.path):
            try:
                with path.open("rb") as handle:
                    size = handle.seek(0, 2)
                    handle.seek(max(0, size - 2 * 1024 * 1024))
                    if size > 2 * 1024 * 1024:
                        handle.readline()
                    lines = handle.read().decode("utf-8", "replace").splitlines()
                for line in lines:
                    match = _SAVED.match(line)
                    if match:
                        ts, source, level, message = match.groups()
                        self.entries.append(LogEntry(ts, source, level or infer_level(message), message))
                self.entries[:] = self.entries[-self.max_entries:]
            except OSError:
                continue

    def snapshot(self, clear_pending=False):
        with self._lock:
            if clear_pending:
                while not self.ui_queue.empty():
                    self.ui_queue.get_nowait()
            return list(self.entries)

    @contextlib.contextmanager
    def listen(self, callback):
        with self._lock:
            self._listeners.append(callback)
        try:
            yield
        finally:
            with self._lock:
                self._listeners.remove(callback)

    def log(self, source: str, message: str, level: str | None = None):
        for line in clean_output(message).replace("\r", "\n").splitlines():
            if not line.strip():
                continue
            severity = infer_level(line) if level is None else level.upper()
            severity = {"WARN": "WARNING", "CRITICAL": "ERROR", "DEBUG": "INFO"}.get(severity, severity)
            if severity not in LEVELS:
                severity = "INFO"
            item = LogEntry(time.strftime("%Y-%m-%d %H:%M:%S"), str(source), severity, line)
            with self._lock:
                self.entries.append(item)
                del self.entries[:max(0, len(self.entries) - self.max_entries)]
                if self.ui_queue.full():
                    try:
                        self.ui_queue.get_nowait()
                    except queue.Empty:
                        pass
                self.ui_queue.put_nowait(item)
                listeners = tuple(self._listeners)
            with self._file_lock:
                self._write_file(item)
            for callback in listeners:
                try:
                    callback(item)
                except Exception:
                    pass

    def _write_file(self, item):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > 2 * 1024 * 1024:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(format_entry(item) + "\n")
        except OSError:
            # Do not recurse into logging if the log volume is unavailable.
            pass


def format_entry(item: LogEntry) -> str:
    return f"[{item.timestamp}] [{item.source}] [{item.level}] {item.message}"


@contextlib.contextmanager
def log_source(source):
    token = _SOURCE.set(source)
    try:
        yield
    finally:
        _SOURCE.reset(token)


class LogStream(io.TextIOBase):
    """Buffer print fragments per thread; classify stderr instead of calling it ERROR."""
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger):
        super().__init__()
        self.logger = logger
        self._local = threading.local()

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, text):
        text = str(text)
        buffered = getattr(self._local, "buffer", "") + text.replace("\r", "\n")
        lines = buffered.split("\n")
        self._local.buffer = lines.pop()
        for line in lines:
            self._emit(line)
        return len(text)

    def _emit(self, line):
        if not line.strip():
            return
        trace = getattr(self._local, "trace", False)
        if "Traceback (most recent call last)" in line:
            trace = True
        self.logger.log(_SOURCE.get(), line, "ERROR" if trace else None)
        self._local.trace = trace and not bool(_EXCEPTION.search(line))

    def flush(self):
        line = getattr(self._local, "buffer", "")
        self._local.buffer = ""
        self._emit(line)


class AppLogHandler(logging.Handler):
    def __init__(self, logger):
        super().__init__(logging.INFO)
        self.logger = logger

    def emit(self, record):
        source = getattr(record, "source", None) or ("内核" if record.name == "pdf2zh.kernel" else _SOURCE.get())
        level = "ERROR" if record.levelno >= logging.ERROR else "WARNING" if record.levelno >= logging.WARNING else "INFO"
        self.logger.log(source, self.format(record), level)


def install_desktop_logging(logger):
    """Install once for the app lifetime, before importing the server."""
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, AppLogHandler):
            return
    # A console handler would write the same record back through LogStream.
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
    root.addHandler(AppLogHandler(logger))
    root.setLevel(logging.INFO)
    sys.stdout = LogStream(logger)
    sys.stderr = LogStream(logger)
    logging.captureWarnings(True)

    def uncaught(exc_type, exc, tb):
        logging.getLogger("pdf2zh.desktop").error("未处理的异常", exc_info=(exc_type, exc, tb))

    sys.excepthook = uncaught
    threading.excepthook = lambda args: uncaught(args.exc_type, args.exc_value, args.exc_traceback)
