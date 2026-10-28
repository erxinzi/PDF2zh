"""High DPI setup and screen-aware desktop dimensions."""
from __future__ import annotations

import ctypes
import os


def enable_high_dpi():
    """Call before creating any Tk window; the exe also declares this in its manifest."""
    if os.name != "nt":
        return "native"
    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        if user32.AreDpiAwarenessContextsEqual(user32.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4)):
            return "PerMonitorV2"
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "PerMonitorV2"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "PerMonitor"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except (AttributeError, OSError):
        return "native"


def window_dpi(root):
    if os.name == "nt":
        try:
            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes = [ctypes.c_void_p]
            get_dpi.restype = ctypes.c_uint
            dpi = int(get_dpi(root.winfo_id()))
            if dpi:
                return dpi
        except (AttributeError, OSError):
            pass
    return max(72, round(float(root.winfo_fpixels("1i"))))


def work_area(root):
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT), ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user32.MonitorFromWindow(root.winfo_id(), 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.work
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError):
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def layout_dimensions(width, height, dpi=96):
    scale = max(0.75, dpi / 96)
    available_width = max(1, width - round(32 * scale))
    available_height = max(1, height - round(64 * scale))
    desired_width = min(round(1280 * scale), available_width)
    desired_height = min(round(860 * scale), available_height)
    return {
        "width": desired_width, "height": desired_height,
        "min_width": min(round(1000 * scale), available_width),
        "min_height": min(round(680 * scale), available_height),
        "scale": scale,
    }


def configure_window(root):
    dpi = window_dpi(root)
    root.tk.call("tk", "scaling", dpi / 72)
    x, y, width, height = work_area(root)
    layout = layout_dimensions(width, height, dpi)
    left = x + max(0, (width - layout["width"]) // 2)
    top = y + max(0, (height - layout["height"]) // 2 - round(16 * layout["scale"]))
    root.geometry(f"{layout['width']}x{layout['height']}+{left}+{top}")
    root.minsize(layout["min_width"], layout["min_height"])
    root.resizable(True, True)
    return {**layout, "dpi": dpi}
