"""Cooperative task checkpoints and native control of managed child trees."""
from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import threading


class TaskCancelled(Exception):
    pass


def _descendants(root):
    from ctypes import wintypes as w
    class ENTRY(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD), ("exe", w.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(ENTRY)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = ENTRY(); entry.size = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.pid] = entry.parent
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    family = [root]
    for parent in family:
        family.extend(pid for pid, ppid in parents.items() if ppid == parent and pid not in family)
    return family


class ProcessTree:
    def __init__(self, process):
        self.process = process
        self.suspended = []

    def _native(self, pid, function):
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not handle:
            # A descendant can exit while enumerating the tree.
            if ctypes.get_last_error() == 87:
                return False
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            method = getattr(ctypes.WinDLL("ntdll"), function)
            method.argtypes = [w.HANDLE]
            method.restype = w.LONG
            status = method(handle)
            if status < 0:
                raise OSError(f"{function} failed: {status:#x}")
            return True
        finally:
            kernel.CloseHandle(handle)

    def pause(self):
        if self.process.poll() is not None or self.suspended:
            return
        if os.name == "nt":
            try:
                # Freeze the parent first so it cannot create new descendants.
                for pid in _descendants(self.process.pid):
                    if self._native(pid, "NtSuspendProcess"):
                        self.suspended.append(pid)
                # Catch children created immediately before suspending the parent.
                for pid in _descendants(self.process.pid):
                    if pid not in self.suspended and self._native(pid, "NtSuspendProcess"):
                        self.suspended.append(pid)
            except Exception:
                self.resume()
                raise
        else:
            os.kill(self.process.pid, signal.SIGSTOP)
            self.suspended = [self.process.pid]

    def resume(self):
        pending, self.suspended = self.suspended, []
        for pid in reversed(pending):
            try:
                if os.name == "nt":
                    self._native(pid, "NtResumeProcess")
                else:
                    os.kill(pid, signal.SIGCONT)
            except OSError:
                if self.process.poll() is None and pid == self.process.pid:
                    raise

    def cancel(self):
        if os.name == "nt":
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            kernel.OpenProcess.restype = w.HANDLE
            kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
            kernel.CloseHandle.argtypes = [w.HANDLE]
            if self.process.poll() is None:
                self.pause()
                try:
                    for pid in reversed(_descendants(self.process.pid)[1:]):
                        handle = kernel.OpenProcess(1, False, pid)
                        if handle:
                            try:
                                if not kernel.TerminateProcess(handle, 1):
                                    raise ctypes.WinError(ctypes.get_last_error())
                            finally:
                                kernel.CloseHandle(handle)
                        elif ctypes.get_last_error() != 87:
                            raise ctypes.WinError(ctypes.get_last_error())
                    self.process.kill()
                except Exception:
                    self.resume()
                    raise
            self.suspended = []
        else:
            self.resume()
            if self.process.poll() is None:
                self.process.kill()


class TaskControl:
    def __init__(self):
        self.lock = threading.RLock()
        self.ready = threading.Event(); self.ready.set()
        self.cancelled = False
        self.paused = False
        self.tree = None

    def checkpoint(self):
        while not self.ready.wait(0.1):
            if self.cancelled:
                raise TaskCancelled("任务已取消")
        if self.cancelled:
            raise TaskCancelled("任务已取消")

    def pause(self):
        with self.lock:
            if self.cancelled:
                raise TaskCancelled("任务已取消")
            if self.tree:
                self.tree.pause()
            self.paused = True
            self.ready.clear()

    def resume(self):
        with self.lock:
            if self.tree:
                self.tree.resume()
            self.paused = False
            self.ready.set()

    def cancel(self):
        with self.lock:
            if self.tree:
                self.tree.cancel()
            self.cancelled = True
            self.paused = False
            self.ready.set()

    @contextlib.contextmanager
    def bind(self, process):
        with self.lock:
            self.tree = ProcessTree(process)
            if self.cancelled:
                self.tree.cancel()
            elif self.paused:
                self.tree.pause()
        try:
            yield
            self.checkpoint()
        finally:
            with self.lock:
                self.tree = None
