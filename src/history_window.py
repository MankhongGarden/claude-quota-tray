"""
Full history window: 2 progress bars on top + 24-hour usage chart.
Uses shared widgets from bar_widget.py.

Runs as a ``Toplevel`` of the process-wide Tk root managed by
``tk_host``. See ``tk_host`` for the rationale behind the centralised
Tk lifecycle.
"""

from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from typing import Callable, Optional

import dpi
import history
import tk_host
from bar_widget import (
    BAR_HEIGHT, BAR_HEIGHT_COMPACT, BG, BTN_BG, BTN_BG_ACTIVE, MUTED, TEXT,
    apply_bar, build_bar, format_burn, ui_font,
)
from i18n import t


_open_window: Optional[tk.Toplevel] = None

SnapshotFetcher = Callable[[], dict]


def show(account_id: str, account_name: str,
         get_data: Optional[SnapshotFetcher] = None,
         burn: Optional[dict] = None) -> None:
    if not tk_host.is_ready():
        return

    if get_data is None:
        b = burn or {}
        get_data = lambda: {"claims": [], "burn": b}

    tk_host.spawn(lambda root: _build(root, account_id, account_name, get_data))


def _build(root: tk.Tk, account_id: str, account_name: str,
           get_data: SnapshotFetcher) -> None:
    global _open_window
    if _open_window is not None:
        try:
            _open_window.lift()
            _open_window.focus_force()
            return
        except tk.TclError:
            _open_window = None

    win = tk.Toplevel(root)
    win.title(t('window.history_title', name=account_name))
    win.geometry(f"{dpi.px(760, win)}x{dpi.px(560, win)}")
    win.minsize(dpi.px(560, win), dpi.px(420, win))
    win.configure(bg=BG)

    _open_window = win

    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=18, pady=(16, 0))
    title_box = tk.Frame(header, bg=BG)
    title_box.pack(side="left")
    tk.Label(
        title_box, text=t('window.current_usage'),
        font=ui_font(13, "bold"),
        fg=TEXT, bg=BG,
    ).pack(side="left")
    plan_lbl = tk.Label(
        title_box, text="", font=ui_font(10, "bold"),
        fg="#a3b8ff", bg=BG,
    )
    plan_lbl.pack(side="left", padx=(10, 0))
    burn_lbl = tk.Label(
        header, text="", font=ui_font(10),
        fg=MUTED, bg=BG, justify="right",
    )
    burn_lbl.pack(side="right")

    bars_panel = tk.Frame(win, bg=BG)
    bars_panel.pack(fill="x", padx=18, pady=(10, 6))

    # One bar and one chart line per bucket, rebuilt when the set changes
    # (a per-model weekly bucket appears the first time that model is used).
    bars: dict[str, dict] = {}
    layout: list[str] = []

    # Which surfaces consumed the weekly window. The endpoint reports it and
    # nothing else in the UI has room for it.
    breakdown_lbl = tk.Label(win, text="", font=ui_font(9), fg=MUTED, bg=BG,
                             anchor="w")
    breakdown_lbl.pack(fill="x", padx=18, pady=(6, 0))

    tk.Label(
        win, text=t('window.last_24h'),
        font=ui_font(11, "bold"),
        fg=TEXT, bg=BG,
    ).pack(anchor="w", padx=18, pady=(14, 2))

    # Footer before the chart: the canvas expands, so packing it first would
    # let a tall bucket list push the legend and buttons off-window.
    footer = tk.Frame(win, bg=BG)
    footer.pack(fill="x", padx=18, pady=(0, 14), side="bottom")
    legend = tk.Frame(footer, bg=BG)
    legend.pack(side="left")

    canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
    canvas.pack(fill="both", expand=True, padx=18, pady=(2, 8))

    def _fit(again: bool = False) -> None:
        try:
            win.update_idletasks()
            floor_w, floor_h = dpi.px(760, win), dpi.px(560, win)
            width = max(win.winfo_width(), win.winfo_reqwidth(), floor_w)
            # Leave room for the taskbar and the title bar.
            screen_room = win.winfo_screenheight() - dpi.px(160, win)
            height = min(
                max(floor_h, win.winfo_reqheight() + dpi.px(_CHART_MIN_HEIGHT, win)),
                screen_room)
            win.geometry(f"{width}x{height}")
            if not again:
                # The first pass measures a layout that is still settling.
                win.after(60, lambda: _fit(True))
        except tk.TclError:
            pass

    def _rebuild(claims: list[dict]) -> None:
        for child in bars_panel.winfo_children():
            child.destroy()
        for child in legend.winfo_children():
            child.destroy()
        bars.clear()
        layout.clear()
        compact = len(claims) > 2
        for idx, claim in enumerate(claims):
            bar = build_bar(bars_panel, claim.get("label") or claim["key"],
                            compact=compact)
            bar["frame"].pack(fill="x", pady=(0, 10) if idx < len(claims) - 1 else 0)
            bars[claim["key"]] = bar
            layout.append(claim["key"])
            _legend_swatch(legend, series_color(claim["key"], idx),
                           claim.get("label") or claim["key"])
        # Keep the chart readable: grow the window instead of squeezing it.
        _fit()

    def _refresh_all():
        data = get_data()
        claims = data.get("claims") or _legacy_claims(data)
        if [c["key"] for c in claims] != layout:
            _rebuild(claims)
        for claim in claims:
            bar = bars.get(claim["key"])
            if bar:
                apply_bar(bar, claim.get("pct"), claim.get("reset"))
        burn_lbl.configure(text=format_burn(data.get("burn") or {}))
        plan = data.get("plan")
        plan_lbl.configure(text=("· " + plan) if plan else "")
        breakdown_lbl.configure(text=_breakdown_text(data.get("weekly_breakdown")))
        _redraw_chart(canvas, account_id, list(layout))

    refresh_btn = tk.Button(
        footer, text=t('common.refresh'),
        command=_refresh_all,
        bg=BTN_BG, fg=TEXT, relief="flat",
        activebackground=BTN_BG_ACTIVE, activeforeground=TEXT,
        padx=14, pady=4, cursor="hand2",
    )
    refresh_btn.pack(side="right")

    canvas.bind("<Configure>",
                lambda _e: _redraw_chart(canvas, account_id, list(layout)))
    win.after(50, _refresh_all)

    def _auto():
        if not win.winfo_exists():
            return
        try:
            _refresh_all()
        except tk.TclError:
            return
        win.after(30_000, _auto)

    win.after(30_000, _auto)

    def on_close():
        global _open_window
        if _open_window is win:
            _open_window = None
        try:
            win.destroy()
        except tk.TclError:
            pass

    win.protocol("WM_DELETE_WINDOW", on_close)


def _legend_swatch(parent: tk.Frame, color: str, label: str) -> None:
    box = tk.Frame(parent, bg=BG)
    box.pack(side="left", padx=(0, 18))
    tk.Frame(box, width=dpi.px(14, parent), height=dpi.px(14, parent),
             bg=color).pack(side="left", padx=(0, dpi.px(6, parent)))
    tk.Label(box, text=label, fg=MUTED, bg=BG,
             font=ui_font(9)).pack(side="left")


# Vertical room the 24h chart keeps for itself once the bars are laid out.
_CHART_MIN_HEIGHT = 240

_SERIES_COLORS = ("#4ade80", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa", "#2dd4bf")


def series_color(key: str, index: int) -> str:
    """Stable colour per bucket: 5h green, weekly blue, then the rest."""
    fixed = {"five_hour": _SERIES_COLORS[0], "seven_day": _SERIES_COLORS[1]}
    if key in fixed:
        return fixed[key]
    return _SERIES_COLORS[2 + (index % (len(_SERIES_COLORS) - 2))]


def _breakdown_text(rows) -> str:
    """One line naming which surfaces consumed the weekly window."""
    parts = [f"{row.get('label')} {row.get('pct')}%" for row in (rows or [])
             if row.get("pct")]
    return f"{t('bar.weekly_short')}: " + " · ".join(parts[:4]) if parts else ""


def _legacy_claims(data: dict) -> list[dict]:
    """Fall back to the two fixed windows when no claim list is supplied."""
    out = []
    for key, pct_key, reset_key, label_key in (
        ("five_hour", "session_pct", "session_reset", 'bar.session_label'),
        ("seven_day", "weekly_pct", "weekly_reset", 'bar.weekly_label'),
    ):
        if data.get(pct_key) is not None:
            out.append({
                "key": key,
                "label": t(label_key),
                "pct": data.get(pct_key),
                "reset": data.get(reset_key),
            })
    return out


def _redraw_chart(canvas: tk.Canvas, account_id: str,
                  keys: Optional[list[str]] = None) -> None:
    canvas.delete("all")
    w = canvas.winfo_width()
    h = canvas.winfo_height()
    if w < 50 or h < 50:
        return

    rows = history.recent_claims(24, account_id)
    if not keys:
        keys = history.known_keys(24, account_id)
    S = dpi.scale(canvas)
    pad_l, pad_r, pad_t, pad_b = (int(round(v * S)) for v in (44, 12, 8, 24))
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    for pct in (0, 25, 50, 75, 100):
        y = pad_t + plot_h - (pct / 100.0) * plot_h
        canvas.create_line(pad_l, y, pad_l + plot_w, y,
                           fill="#2d2d2d", width=max(1, int(round(S))))
        canvas.create_text(pad_l - int(round(6 * S)), y, text=f"{pct}%",
                           anchor="e", fill=MUTED,
                           font=ui_font(8))

    now = time.time()
    start = now - 24 * 3600
    for hours_ago in (24, 18, 12, 6, 0):
        ts = now - hours_ago * 3600
        x = pad_l + ((ts - start) / max(now - start, 1)) * plot_w
        label = datetime.fromtimestamp(ts).strftime("%H:%M")
        canvas.create_text(x, pad_t + plot_h + int(round(12 * S)), text=label,
                           fill=MUTED, font=ui_font(8))

    if not rows:
        canvas.create_text(
            pad_l + plot_w / 2, pad_t + plot_h / 2,
            text=t('window.no_history'),
            fill=MUTED, font=ui_font(10),
        )
        return

    def to_xy(ts: float, pct: int):
        x = pad_l + ((ts - start) / max(now - start, 1)) * plot_w
        y = pad_t + plot_h - (pct / 100.0) * plot_h
        return x, y

    for idx, key in enumerate(keys):
        _plot_series(canvas, rows, key, series_color(key, idx), to_xy, S)


def _plot_series(canvas: tk.Canvas, rows: list, key: str, color: str,
                 to_xy, scale: float = 1.0) -> None:
    dot = max(3, int(round(3 * scale)))
    dot_last = max(4, int(round(4 * scale)))
    pts = [to_xy(ts, vals[key]) for ts, vals in rows if key in vals]
    if len(pts) < 2:
        for x, y in pts:
            canvas.create_oval(x - dot, y - dot, x + dot, y + dot,
                               fill=color, outline="")
        return
    flat: list[float] = []
    for x, y in pts:
        flat.extend([x, y])
    canvas.create_line(*flat, fill=color, width=max(2, int(round(2 * scale))),
                       smooth=True)
    last_x, last_y = pts[-1]
    canvas.create_oval(last_x - dot_last, last_y - dot_last,
                       last_x + dot_last, last_y + dot_last,
                       fill=color, outline="")
