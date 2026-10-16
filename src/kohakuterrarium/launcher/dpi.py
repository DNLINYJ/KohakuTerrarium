"""Standalone process DPI setup before either splash toolkit creates windows."""

import ctypes
import sys
import threading

from kohakuterrarium.launcher.log import get_logger


def configure_process_dpi() -> bool:
    if sys.platform != "win32":
        return True
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetProcessDpiAwarenessContext.restype = ctypes.c_int
    user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user.AreDpiAwarenessContextsEqual.restype = ctypes.c_int
    desired = ctypes.c_void_p(-4)
    changed = user.SetProcessDpiAwarenessContext(desired)
    error = 0 if changed else ctypes.get_last_error()
    inherited = []
    probe = threading.Thread(
        target=lambda: inherited.append(user.GetThreadDpiAwarenessContext())
    )
    probe.start()
    probe.join()
    effective = bool(user.AreDpiAwarenessContextsEqual(inherited[0], desired))
    if not effective:
        get_logger().warning(
            "Per-monitor DPI initialization failed (Windows error %s)", error
        )
    return effective
