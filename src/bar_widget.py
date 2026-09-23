"""
Reusable Tk progress-bar widget plus helpers used by both the compact
status popup and the full history window.

The bar is a thin rounded capsule whose fill colour shifts from green
through yellow/orange to red as utilisation rises.
"""

from __future__ import annotations

import tkinter as tk
from typing import Optional

import dpi


# --- Colours -------------------------------------------------------------

BG = "#1e1e1e"
PANEL_BG = "#262626"
TRACK_BG = "#2d2d2d"
TEXT = "#f5f5f5"
MUTED = "#94a3b8"
BTN_BG = "#2d2d2d"
BTN_BG_ACTIVE = "#3a3a3a"

# Flyout palette — the tray panel reads as a surface of the shell, so it is
# dark regardless of the Windows theme (the tray icon and chart colours are
# tuned for these).
FLYOUT_BG = "#17171b"
FLYOUT_TRACK = "#33333c"
FLYOUT_TEXT = "#ebebf0"
FLYOUT_DIM = "#8e8e99"
FLYOUT_LINE = "#2a2a31"
FLYOUT_STALE = "#fbbf24"
FLYOUT_CRIT = "#ef4444"
FLYOUT_OK = "#22c55e"


# --- Font selection ------------------------------------------------------

_FAMILY_CACHE: dict[str, str] = {}

# Preferred family chains. First installed wins.
#
# Noto Sans Thai is deliberately NOT in this chain: its bold weight drops
# vowels and tone marks under Tk here (ลิมิตรอบ renders as ลมตริอบ), and every
# bar label asks for bold. The three below ship with Windows and render Thai
# correctly at both weights.
_THAI_FAMILY_CHAIN = (
    "Leelawadee UI",
    "Tahoma",
    "Segoe UI",
)
_DEFAULT_FAMILY_CHAIN = (
    "Segoe UI",
    "Helvetica",
    "TkDefaultFont",
)


def _resolve_family(chain: tuple[str, ...]) -> str:
    """Return the first installed family from the chain, cached."""
    key = "|".join(chain)
    if key in _FAMILY_CACHE:
        return _FAMILY_CACHE[key]
    try:
        import tkinter.font as tkfont
        installed = set(tkfont.families())
    except Exception:
        installed = set()
    for family in chain:
        if family in installed or family.startswith("Tk"):
            _FAMILY_CACHE[key] = family
            return family
    _FAMILY_CACHE[key] = chain[-1]
    return chain[-1]


def ui_family() -> str:
    """The family to use for UI text in the active language."""
    try:
        from i18n import current_language
        lang = current_language()
    except Exception:
        lang = "en"
    return _resolve_family(_THAI_FAMILY_CHAIN if lang == "th"
                           else _DEFAULT_FAMILY_CHAIN)


def ui_font(size: int, weight: str = "normal") -> tuple:
    """Pick a Tk font tuple, using Thai-friendly families when Thai is active.

    Size is in points: Tk multiplies it by the interpreter scaling, so these
    grow correctly once the process is DPI aware. Never mix this with the
    device-pixel sizes from `flyout_font` inside one window.
    """
    return (ui_family(), size, weight)


def flyout_font(du: int, win, bold: bool = False, mono: bool = False) -> tuple:
    """
    Font sized in device pixels, for text placed by hand on a Canvas.

    A negative Tk size means pixels, which is what hand-computed layouts need
    — point sizes would be scaled a second time by the interpreter.
    """
    family = "Consolas" if mono else ui_family()
    return (family, -dpi.px(du, win), "bold" if bold else "normal")


def bar_color(pct: int) -> str:
    """Return the fill colour for a given utilisation percentage."""
    if pct < 50:
        return "#22c55e"   # green
    if pct < 75:
        return "#facc15"   # yellow
    if pct < 90:
        return "#f97316"   # orange
    return "#ef4444"       # red


def color_emoji(pct: Optional[int]) -> str:
    """Coloured-dot emoji corresponding to the bar colour. For menu labels."""
    if pct is None:
        return "⚪"
    if pct < 50:
        return "🟢"
    if pct < 75:
        return "🟡"
    if pct < 90:
        return "🟠"
    return "🔴"


def unicode_bar(pct: Optional[int], width: int = 10) -> str:
    """ASCII-art progress bar suitable for native menu labels."""
    if pct is None:
        return "░" * width
    pct = max(0, min(100, int(pct)))
    filled = int(round((pct / 100.0) * width))
    return ("█" * filled) + ("░" * (width - filled))


def _lighten(hex_color: str, amount: float) -> str:
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    r = min(255, int(r + (255 - r) * amount))
    g = min(255, int(g + (255 - g) * amount))
    b = min(255, int(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


def blend(color: str, onto: str, amount: float) -> str:
    """Mix `color` into `onto` by `amount` (0 = onto, 1 = color)."""
    a = color.lstrip("#")
    b = onto.lstrip("#")
    out = []
    for i in (0, 2, 4):
        ca, cb = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(max(0, min(255, int(round(cb + (ca - cb) * amount)))))
    return "#{:02x}{:02x}{:02x}".format(*out)


def _rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    r = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def paint_bar(canvas: tk.Canvas, pct: Optional[int]) -> None:
    """Paint the bar's track and fill on a canvas."""
    canvas.delete("all")
    w = canvas.winfo_width()
    h = canvas.winfo_height()
    if w < 4 or h < 4:
        return

    _rounded_rect(canvas, 0, 0, w, h, radius=h / 2, fill=TRACK_BG, outline="")
    if pct is None or pct <= 0:
        return

    fill_w = max(int(round((min(pct, 100) / 100.0) * w)), int(h))
    color = bar_color(pct)
    _rounded_rect(canvas, 0, 0, fill_w, h, radius=h / 2, fill=color, outline="")

    if fill_w > h:
        canvas.create_rectangle(
            int(h / 2), 2, fill_w - int(h / 2), int(h / 2),
            fill=_lighten(color, 0.18), outline="",
        )


# Height one bar occupies at 96 DPI. Callers that need real pixels use the
# helpers below; the constants stay for older import sites.
BAR_HEIGHT = 84
BAR_HEIGHT_COMPACT = 58


def bar_height(win) -> int:
    return dpi.px(BAR_HEIGHT, win)


def bar_height_compact(win) -> int:
    return dpi.px(BAR_HEIGHT_COMPACT, win)


def build_bar(parent, label_text: str, compact: bool = False) -> dict:
    """Create one labelled progress bar and return its widget refs.

    `compact` shrinks the row so four or five buckets still fit a window
    sized for two.
    """
    pad = dpi.px(14, parent)
    frame = tk.Frame(parent, bg=PANEL_BG)

    top = tk.Frame(frame, bg=PANEL_BG)
    top.pack(fill="x", padx=pad,
             pady=(dpi.px(6, parent), dpi.px(2, parent)) if compact
             else (dpi.px(10, parent), dpi.px(4, parent)))

    tk.Label(
        top, text=label_text, font=ui_font(9 if compact else 10, "bold"),
        fg=TEXT, bg=PANEL_BG,
    ).pack(side="left")

    pct_w = tk.Label(
        top, text="—", font=ui_font(14 if compact else 22, "bold"),
        fg=TEXT, bg=PANEL_BG,
    )
    pct_w.pack(side="right")

    # width=1 matters: with no width Tk asks for its 10-centimetre default,
    # which made every window that holds a bar request ~380px more than it
    # was given and clip its own content.
    canvas = tk.Canvas(frame, width=1,
                       height=dpi.px(12 if compact else 18, parent),
                       bg=PANEL_BG, highlightthickness=0, bd=0)
    canvas.pack(fill="x", padx=pad)

    reset_w = tk.Label(
        frame, text="—", font=ui_font(8 if compact else 9),
        fg=MUTED, bg=PANEL_BG, anchor="w",
    )
    reset_w.pack(fill="x", padx=pad,
                 pady=(dpi.px(2, parent), dpi.px(6, parent)) if compact
                 else (dpi.px(4, parent), dpi.px(10, parent)))

    state = {
        "frame": frame, "canvas": canvas,
        "pct": pct_w, "reset": reset_w,
        "value": None,
    }

    def _redraw(_event=None):
        paint_bar(canvas, state["value"])

    canvas.bind("<Configure>", _redraw)
    state["redraw"] = _redraw
    return state


def apply_bar(bar: dict, pct: Optional[int], reset_seconds: Optional[int]) -> None:
    from i18n import t
    bar["value"] = pct
    if pct is None:
        bar["pct"].configure(text="—", fg=MUTED)
        bar["reset"].configure(text=t('bar.no_data'))
    else:
        bar["pct"].configure(text=f"{pct}%", fg=bar_color(pct))
        if reset_seconds is None:
            bar["reset"].configure(text=t('bar.resets_unknown'))
        else:
            bar["reset"].configure(
                text=t('bar.resets_in', time=fmt_seconds(reset_seconds))
            )
    bar["redraw"]()


def fmt_seconds(seconds: Optional[int]) -> str:
    from api_client import format_reset
    return format_reset(seconds)


def format_burn(burn: dict) -> str:
    from claims import sorted_keys
    from i18n import claim_label, t
    lines = []
    # "session"/"weekly" are aliases of five_hour/seven_day in the same dict.
    keys = [k for k in (burn or {}) if k not in ("session", "weekly")]
    # Header space is tight — show only the three fastest-filling buckets.
    keys = sorted(keys, key=lambda k: -((burn.get(k) or {}).get("rate") or 0))[:3]
    for key in sorted_keys(keys):
        info = (burn or {}).get(key, {}) or {}
        rate = info.get("rate")
        eta = info.get("eta_seconds")
        # Flat buckets say nothing useful and there are several of them now.
        if rate is None or rate <= 0.05:
            continue
        label = claim_label(key)
        if eta is not None:
            lines.append(
                t('bar.burn_full_in', label=label, rate=rate,
                  eta=fmt_seconds(eta))
            )
        else:
            lines.append(t('bar.burn_no_eta', label=label, rate=rate))
    return "\n".join(lines) if lines else t('bar.burn_collecting')
