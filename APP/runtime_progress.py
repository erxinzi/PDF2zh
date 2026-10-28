"""Versioned progress messages shared by the kernel child and desktop server."""
from __future__ import annotations

import json
import math
import sys
import threading
import time


PREFIX = "@@PDF2ZH_PROGRESS@@"


def decode_progress(line):
    position = line.find(PREFIX)
    if position < 0:
        return None
    try:
        event, _ = json.JSONDecoder().raw_decode(line[position + len(PREFIX):].lstrip())
        if not isinstance(event, dict) or event.get("version") != 1:
            return None
        value = event["progress"]
        if isinstance(value, bool):
            return None
        percent = float(value)
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            return None
        event["progress"] = percent
        event["stage"] = str(event.get("stage") or "处理中")[:200]
        return event
    except (ValueError, TypeError, KeyError):
        return None


class ProgressEmitter:
    def __init__(self, stream=None, clock=time.monotonic):
        self.stream = stream if stream is not None else sys.stdout
        self.clock = clock
        self.previous = None
        self.last_time = 0.0
        self.percent = 0.0
        self.lock = threading.Lock()

    def __call__(self, event):
        if event.get("type") not in ("progress_start", "progress_update", "progress_end", "finish", "error"):
            return
        value = event.get("overall_progress", 100 if event.get("type") == "finish" else self.percent)
        try:
            percent = float(value)
        except (ValueError, TypeError):
            return
        if not math.isfinite(percent):
            return
        with self.lock:
            self.percent = max(self.percent, min(100.0, max(0.0, percent)))
            stage = str(event.get("stage") or (self.previous[1] if event.get('type') == 'error' and self.previous else None) or "生成文件完成")
            key = (int(self.percent), stage)
            now = self.clock()
            if self.previous == key and now - self.last_time < 0.5 and event.get("type") not in ("finish", "error"):
                return
            payload = {"version": 1, "type": event["type"], "progress": round(self.percent, 2),
                       "stage": stage, "current": event.get("stage_current", 0), "total": event.get("stage_total", 0)}
            if event['type'] == 'error':
                error = event.get('error')
                payload['error'] = str(error or '翻译内核报告失败')[:6000]
                kind = event.get('errorType') or (type(error).__name__ if isinstance(error, BaseException) else 'KernelTranslationError')
                payload['errorType'] = str(kind)[:128]
            text = PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            # A separate, flushed line survives merged stderr and pipe buffering.
            self.stream.write("\n" + text + "\n")
            self.stream.flush()
            self.previous, self.last_time = key, now
