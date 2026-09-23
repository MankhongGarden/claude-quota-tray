"""
Process DPI awareness, and the scale factor the windows draw against.

Windows hands an unaware process a virtualised 96-DPI desktop and then
bitmap-stretches its windows, which is why the panels used to look soft on a
125% display. Declaring system DPI awareness makes Tk draw at real pixels.

`enable()` MUST run before `tk.Tk()`. Called afterwards it still returns
success and Win32 metrics do change, but the Tk interpreter has already
cached the old scaling: the UI comes out crisp and ~20% too small, silently.

It asks for system awareness, but pythonw.exe already declares per-monitor
awareness in its manifest, in which case the request is refused and the
process stays per-monitor aware. That is the harder case: Tk starts before
any window exists, sees 96 DPI and never revisits it, so `scale()` asks the
display directly and `sync_tk_scaling()` corrects the interpreter once the
root exists. Tk 8.6 has no WM_DPICHANGED handling, so a window dragged to a
monitor at another scale keeps the scale it was built with.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional


_enabled: bool | None = None


def enable() -> bool:
    """Declare system DPI awareness. Idempotent; safe to call from anywhere."""
    global _enabled
    if _enabled is not None:
        return _enabled
    _enabled = False
    try:
        # PROCESS_SYSTEM_DPI_AWARE = 1. Returns E_ACCESSDENIED if awareness
        # was already set (by a manifest, or by a second call) — that is a
        # success for our purposes.
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(1)
        _enabled = hr in (0, -2147024891)
    except (AttributeError, OSError):
        try:
            _enabled = bool(ctypes.windll.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            _enabled = False
    return _enabled


def window_dpi(win) -> Optional[int]:
    """The DPI of the display `win` is on, or None if Windows cannot say.

    GetDpiForWindow answers 96 in an unaware process (which is the honest
    answer there — the DWM will stretch the result) and the real monitor DPI
    otherwise, including in a per-monitor-aware process where Tk itself still
    believes it is at 96.
    """
    try:
        user32 = ctypes.windll.user32
        # Declared explicitly: with the default int marshalling a 64-bit HWND
        # is truncated, GetDpiForWindow then answers 0, and the whole layout
        # quietly falls back to 1.0.
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        hwnd = user32.GetAncestor(wintypes.HWND(win.winfo_id()), 2)  # GA_ROOT
        return int(user32.GetDpiForWindow(hwnd)) or None
    except Exception:
        return None


def scale(win) -> float:
    """
    Device pixels per logical pixel for `win`.

    The larger of what Tk believes and what the display reports. Tk reads the
    DPI once, when the interpreter starts: a process that is per-monitor aware
    (pythonw.exe declares this in its manifest) has no window at that moment,
    so Tk settles on 96 and every hand-computed layout would come out ~20%
    too small while looking perfectly crisp.
    """
    best = 1.0
    try:
        best = max(best, float(win.winfo_fpixels("1i")) / 96.0)
    except Exception:
        pass
    monitor = window_dpi(win)
    if monitor:
        best = max(best, monitor / 96.0)
    return best


def sync_tk_scaling(root) -> float:
    """
    Teach Tk the real DPI, so point-sized fonts and paddings match.

    Tk sizes fonts given in points against its own idea of the screen DPI.
    Left at 96 in a per-monitor-aware process, every dialog renders small.
    Returns the scale that is now in effect.
    """
    factor = scale(root)
    try:
        if abs(float(root.winfo_fpixels("1i")) - factor * 96.0) > 1.0:
            root.tk.call("tk", "scaling", factor * 96.0 / 72.0)
    except Exception:
        pass
    return factor


def px(n: float, win) -> int:
    """Logical units -> device pixels for `win`."""
    return int(round(n * scale(win)))


def assert_landed(root, log_path=None) -> bool:
    """
    True when awareness took effect before Tk started.

    Compares what Tk thinks the screen is against the real device width.
    GetSystemMetrics is virtualised for unaware processes and reports the
    same number either way, so it cannot answer this; GetDeviceCaps with
    DESKTOPHORZRES can.
    """
    try:
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        try:
            desktop_w = gdi32.GetDeviceCaps(hdc, 118)  # DESKTOPHORZRES
        finally:
            user32.ReleaseDC(0, hdc)
        tk_w = int(root.winfo_screenwidth())
    except Exception:
        return True

    if desktop_w <= 0 or tk_w == desktop_w:
        return True

    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] dpi: awareness "
                    f"landed late — Tk sees {tk_w}px, device is {desktop_w}px; "
                    f"windows will be crisp but undersized\n"
                )
        except OSError:
            pass
    return False
