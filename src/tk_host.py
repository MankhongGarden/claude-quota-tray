"""
Process-wide Tk host.

Owns the ONE tk.Tk() interpreter that lives for the lifetime of the
process and runs its mainloop on the main thread. All popup windows
(status_window, history_window, settings_dialogs) must be created as
``tk.Toplevel(tk_host.root())`` and must schedule their construction
via ``tk_host.spawn(builder)`` so the call happens on the Tk thread.

Why this exists
---------------
Tk/Tcl ties its interpreter to the thread that created it. Calling
``tk.Tk()`` from a worker thread, or creating multiple ``tk.Tk()``
roots in the same process, eventually triggers a hard ``Tcl_Panic``
inside ``tcl86t.dll`` (exception 0x80000003 / STATUS_BREAKPOINT) — the
process dies with no Python traceback because pythonw swallows it.

This module exists so the rest of the codebase can stay structurally
the same (modules continue to "open windows") while the Tk lifecycle
is centralised on one well-known thread.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import traceback
from typing import Callable, Optional


_root: Optional[tk.Tk] = None
_stop_requested = False


def ensure() -> tk.Tk:
    """Create the hidden Tk root if it doesn't exist yet.

    Must be called from the thread that will subsequently run ``run()``
    (the main thread). Idempotent.
    """
    global _root
    if _root is not None:
        return _root
    r = tk.Tk()
    r.withdraw()
    r.title("")
    # The root is intentionally hidden — closing it would tear down the
    # whole Tk subsystem, so swallow the WM_DELETE_WINDOW protocol.
    r.protocol("WM_DELETE_WINDOW", lambda: None)
    _root = r
    return r


def root() -> tk.Tk:
    """Return the live Tk root. Raises if ``ensure()`` hasn't been called."""
    if _root is None:
        raise RuntimeError("tk_host.ensure() must be called before root()")
    return _root


def is_ready() -> bool:
    return _root is not None


def spawn(builder: Callable[[tk.Tk], None]) -> None:
    """Schedule a popup-builder to run on the Tk thread.

    Safe to call from any thread. ``builder`` receives the hidden root
    and is responsible for creating its own ``Toplevel`` (and managing
    its own lifecycle).
    """
    if _root is None:
        return
    try:
        _root.after_idle(lambda: _safe_call(builder))
    except RuntimeError:
        pass


def _safe_call(builder: Callable[[tk.Tk], None]) -> None:
    try:
        builder(_root)
    except Exception:
        _log("spawn")


def _log(where: str) -> None:
    try:
        import settings as user_settings
        log_path = user_settings.SETTINGS_DIR / "error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] tk_host:{where}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def run() -> None:
    """Block the calling thread (must be main) running Tk mainloop.

    Returns when ``stop()`` has been requested and the event loop has
    drained.
    """
    ensure()
    try:
        _root.mainloop()
    finally:
        # Best-effort interpreter teardown — keeps WER quiet on exit.
        try:
            _root.destroy()  # type: ignore[union-attr]
        except Exception:
            pass


def stop() -> None:
    """Schedule a clean shutdown of the Tk mainloop. Safe from any thread."""
    global _stop_requested
    if _root is None or _stop_requested:
        return
    _stop_requested = True
    try:
        _root.after(0, _root.quit)
    except RuntimeError:
        pass
