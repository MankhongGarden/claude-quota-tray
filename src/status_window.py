"""
Compact status popup shown on left-click of the tray icon.

Just the two progress bars and the burn-rate summary — no chart.
Auto-refreshes every 5 seconds while open.

Runs as a ``Toplevel`` of the process-wide Tk root managed by
``tk_host``. Never creates ``tk.Tk()`` directly — see ``tk_host`` for
the rationale (Tcl/Tk interpreter thread-affinity invariant).
"""

from __future__ import annotations

import time
import tkinter as tk
import traceback
from typing import Callable, Optional

import tk_host
from bar_widget import (
    BG, BTN_BG, BTN_BG_ACTIVE, MUTED, TEXT,
    apply_bar, build_bar, format_burn, ui_font,
)
from i18n import t
import settings as user_settings


_open_window: Optional[tk.Toplevel] = None

SnapshotFetcher = Callable[[], dict]


def _log_error(where: str) -> None:
    try:
        log = user_settings.SETTINGS_DIR / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(
                f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] status_window:{where}\n"
            )
            f.write(traceback.format_exc())
    except Exception:
        pass


def show(account_name: str, get_data: SnapshotFetcher) -> bool:
    """Schedule the compact status popup to open on the Tk thread.

    Returns True if the request was scheduled (Tk host alive). The
    actual window will appear on the next Tk idle tick.
    """
    if not tk_host.is_ready():
        return False
    tk_host.spawn(lambda root: _build(root, account_name, get_data))
    return True


def _build(root: tk.Tk, account_name: str, get_data: SnapshotFetcher) -> None:
    global _open_window
    if _open_window is not None:
        try:
            _bring_to_front(_open_window)
            return
        except tk.TclError:
            _open_window = None

    try:
        win = tk.Toplevel(root)
    except Exception:
        _log_error("toplevel_create")
        return

    win.title(t('window.status_title', name=account_name))
    win.configure(bg=BG)
    win.minsize(360, 240)

    w, h = 420, 300
    try:
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = max(0, sw - w - 32)
        y = max(0, sh - h - 80)
    except tk.TclError:
        x, y = 100, 100
    win.geometry(f"{w}x{h}+{x}+{y}")

    try:
        win.attributes("-topmost", True)
        win.after(400, lambda: _safe_set_topmost(win, False))
    except tk.TclError:
        pass

    _open_window = win
    _build_widgets(win, account_name, get_data)


def _bring_to_front(win: tk.Toplevel) -> None:
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
        win.attributes("-topmost", True)
        win.after(300, lambda: _safe_set_topmost(win, False))
    except tk.TclError:
        pass


def _build_widgets(win: tk.Toplevel, account_name: str,
                   get_data: SnapshotFetcher) -> None:
    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=14, pady=(12, 0))
    title_box = tk.Frame(header, bg=BG)
    title_box.pack(side="left")
    tk.Label(
        title_box, text=account_name,
        font=ui_font(11, "bold"),
        fg=TEXT, bg=BG,
    ).pack(side="left")
    plan_lbl = tk.Label(
        title_box, text="", font=ui_font(9, "bold"),
        fg="#a3b8ff", bg=BG,
    )
    plan_lbl.pack(side="left", padx=(8, 0))
    burn_lbl = tk.Label(
        header, text="", font=ui_font(9),
        fg=MUTED, bg=BG, justify="right",
    )
    burn_lbl.pack(side="right")

    panel = tk.Frame(win, bg=BG)
    panel.pack(fill="x", padx=14, pady=(8, 4))

    session_bar = build_bar(panel, t('bar.session_label'))
    session_bar["frame"].pack(fill="x", pady=(0, 8))
    weekly_bar = build_bar(panel, t('bar.weekly_label'))
    weekly_bar["frame"].pack(fill="x")

    footer = tk.Frame(win, bg=BG)
    footer.pack(fill="x", padx=14, pady=(8, 12), side="bottom")

    def _refresh():
        try:
            data = get_data()
        except Exception:
            data = {}
        apply_bar(session_bar, data.get("session_pct"), data.get("session_reset"))
        apply_bar(weekly_bar, data.get("weekly_pct"), data.get("weekly_reset"))
        burn_lbl.configure(text=format_burn(data.get("burn") or {}))
        plan = data.get("plan")
        plan_lbl.configure(text=("· " + plan) if plan else "")

    tk.Button(
        footer, text=t('common.close'), command=lambda: _on_close(win),
        bg=BTN_BG, fg=TEXT, relief="flat",
        activebackground=BTN_BG_ACTIVE, activeforeground=TEXT,
        padx=14, pady=4, cursor="hand2",
    ).pack(side="right")

    tk.Button(
        footer, text=t('common.refresh'), command=_refresh,
        bg=BTN_BG, fg=TEXT, relief="flat",
        activebackground=BTN_BG_ACTIVE, activeforeground=TEXT,
        padx=14, pady=4, cursor="hand2",
    ).pack(side="right", padx=(0, 8))

    win.after(50, _refresh)

    def _auto():
        if not win.winfo_exists():
            return
        try:
            _refresh()
            win.after(5_000, _auto)
        except tk.TclError:
            return

    win.after(5_000, _auto)

    win.protocol("WM_DELETE_WINDOW", lambda: _on_close(win))
    win.bind("<Escape>", lambda _e: _on_close(win))


def _on_close(win: tk.Toplevel) -> None:
    global _open_window
    try:
        win.destroy()
    except tk.TclError:
        pass
    if _open_window is win:
        _open_window = None


def _safe_set_topmost(win: tk.Toplevel, value: bool) -> None:
    try:
        win.attributes("-topmost", value)
    except tk.TclError:
        pass
