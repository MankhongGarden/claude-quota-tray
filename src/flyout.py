"""
The tray flyout: a borderless panel that appears next to the cursor when the
tray icon is clicked and dismisses itself.

Modelled on TempOverlay: no title bar, no buttons, nothing to close. It hides
when it loses focus, when it is clicked, when Escape is pressed, or when the
icon is clicked again — the same panel is reused for the lifetime of the
process, so the window handle, its rounded corners and its bindings survive.

Everything is drawn by hand on one Canvas: a header band, one row per quota
bucket (label · reset · big value · capsule bar) and a conditional footer that
answers "will this fill before it resets".
"""

from __future__ import annotations

import ctypes
import time
import tkinter as tk
import traceback
from ctypes import wintypes
from typing import Optional

import bar_widget
import dpi
import settings as user_settings
from api_client import format_reset
from claims import sorted_keys
from bar_widget import (
    FLYOUT_BG, FLYOUT_CRIT, FLYOUT_DIM, FLYOUT_LINE, FLYOUT_OK,
    FLYOUT_STALE, FLYOUT_TEXT, FLYOUT_TRACK, bar_color, blend, flyout_font,
)
from i18n import t


# Logical units at 96 DPI; every number goes through dpi.px() before use.
W_DU = 240
PAD_DU = 16
HEADER_DU = 28
ROW_DU = 36
FOOT_DU = 22
GAP_BOTTOM_DU = 8
BAR_DU = 9
GAP_DU = 12

# A left click that dismissed the panel must not reopen it, and the second
# half of a double click must not close what the first half opened.
OPEN_GUARD_MS = 400
HIDE_GUARD_MS = 400

# Backstop for the case where Tk never delivers <FocusOut>.
WATCHDOG_MS = 250

# Ticks only read as landmarks away from the end cap.
MAX_TICK_PCT = 90


# --- Win32 ---------------------------------------------------------------

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi

_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
_user32.MonitorFromPoint.restype = wintypes.HANDLE
_dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
]

GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _top_hwnd(win) -> int:
    """The real top-level HWND; winfo_id() returns the inner Tk child."""
    return int(_user32.GetAncestor(wintypes.HWND(win.winfo_id()), GA_ROOT))


def _round_corners(hwnd: int) -> None:
    pref = ctypes.c_int(DWMWCP_ROUND)
    _dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
        ctypes.byref(pref), ctypes.sizeof(pref),
    )


def _cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _monitor_rects(x: int, y: int):
    """(work, monitor) rects of the display under a point.

    Not SystemParametersInfo(SPI_GETWORKAREA): that always answers for the
    primary display, which puts the panel on the wrong screen.
    """
    mon = _user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(mon, ctypes.byref(info))
    return info.rcWork, info.rcMonitor


def _taskbar_edge(work, monitor) -> str:
    if work.top > monitor.top:
        return "top"
    if work.bottom < monitor.bottom:
        return "bottom"
    if work.left > monitor.left:
        return "left"
    if work.right < monitor.right:
        return "right"
    return "bottom"  # auto-hidden taskbar


def _clamp(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return max(low, min(high, value))


def _force_foreground(hwnd: int) -> None:
    """Bring the panel forward, borrowing input state if Windows refuses."""
    if _user32.SetForegroundWindow(wintypes.HWND(hwnd)):
        return
    try:
        fg = _user32.GetForegroundWindow()
        target = _user32.GetWindowThreadProcessId(fg, None)
        own = ctypes.windll.kernel32.GetCurrentThreadId()
        if target and target != own:
            _user32.AttachThreadInput(own, target, True)
            try:
                _user32.SetForegroundWindow(wintypes.HWND(hwnd))
            finally:
                _user32.AttachThreadInput(own, target, False)
    except OSError:
        pass


def _log(where: str) -> None:
    try:
        path = user_settings.SETTINGS_DIR / "error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] flyout:{where}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


# --- The panel -----------------------------------------------------------

class Flyout:
    """One panel, reused. Built lazily on the Tk thread."""

    def __init__(self, root: tk.Tk, get_data):
        self._get_data = get_data
        self._shown = False
        self._shown_at = 0.0
        self._hidden_at = 0.0
        self._anchor: Optional[tuple[int, int]] = None
        self._watchdog: Optional[str] = None
        self._ticker: Optional[str] = None
        self._data: dict = {}

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)   # Tk adds WS_EX_TOOLWINDOW itself
        self.win.attributes("-topmost", True)
        self.win.configure(bg=FLYOUT_BG)

        self.cv = tk.Canvas(self.win, width=dpi.px(W_DU, self.win), height=1,
                            bg=FLYOUT_BG, highlightthickness=0, bd=0)
        self.cv.pack()

        self.win.update_idletasks()
        self.hwnd = _top_hwnd(self.win)
        _round_corners(self.hwnd)

        # win.bind, never bind_all: a global binding would close this panel
        # whenever the history or settings windows are clicked, and would
        # swallow their Escape key too.
        self.win.bind("<FocusOut>", self._on_focus_out)
        self.win.bind("<Escape>", lambda _e: self.hide())
        self.win.bind("<Button-1>", lambda _e: self.hide())
        self.cv.bind("<Button-1>", lambda _e: self.hide())

    def set_source(self, get_data) -> None:
        """Point the panel at the current data callable for its icon."""
        self._get_data = get_data

    # --- visibility ------------------------------------------------------

    def toggle(self) -> None:
        now = time.monotonic()
        if self._shown:
            if (now - self._shown_at) * 1000 >= OPEN_GUARD_MS:
                self.hide()
            return
        if (now - self._hidden_at) * 1000 < HIDE_GUARD_MS:
            return
        self.show()

    def show(self) -> None:
        close_all_except(self)
        try:
            self._data = self._get_data() or {}
        except Exception:
            _log("show:get_data")
            self._data = {}
        self._anchor = _cursor_pos()
        self.paint()
        self._place()
        self.win.deiconify()
        self.win.lift()
        _force_foreground(self.hwnd)
        try:
            self.win.focus_force()
        except tk.TclError:
            pass
        self._shown = True
        self._shown_at = time.monotonic()
        self._arm_watchdog()
        self._arm_ticker()

    def hide(self) -> None:
        if not self._shown:
            return
        self._shown = False
        self._hidden_at = time.monotonic()
        self._disarm()
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def refresh(self) -> None:
        """New poll data. Never raises the window or takes focus."""
        if not self._shown:
            return
        try:
            self._data = self._get_data() or {}
        except Exception:
            _log("refresh:get_data")
            return
        self.paint()
        self._place(reuse_anchor=True)

    def destroy(self) -> None:
        self._disarm()
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # --- dismiss plumbing ------------------------------------------------

    def _on_focus_out(self, _event=None) -> None:
        # <FocusOut> also fires while withdrawn and right after a click
        # dismiss; re-check one tick later against the real foreground window
        # so those cannot reset the guard timestamps.
        self.win.after(1, self._hide_if_background)

    def _hide_if_background(self) -> None:
        if not self._shown:
            return
        if int(_user32.GetForegroundWindow() or 0) != self.hwnd:
            self.hide()

    def _arm_watchdog(self) -> None:
        if self._watchdog is None:
            self._watchdog = self.win.after(WATCHDOG_MS, self._tick_watchdog)

    def _tick_watchdog(self) -> None:
        self._watchdog = None
        if not self._shown:
            return
        self._hide_if_background()
        if self._shown:
            self._arm_watchdog()

    def _arm_ticker(self) -> None:
        if self._ticker is None:
            self._ticker = self.win.after(1000, self._tick_clock)

    def _tick_clock(self) -> None:
        self._ticker = None
        if not self._shown:
            return
        # Polls are minutes apart, so the countdown has to run locally or it
        # would sit frozen on a stale number.
        try:
            self.paint()
        except Exception:
            _log("tick")
        self._arm_ticker()

    def _disarm(self) -> None:
        for attr in ("_watchdog", "_ticker"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    self.win.after_cancel(handle)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    # --- geometry --------------------------------------------------------

    def _place(self, reuse_anchor: bool = False) -> None:
        w = int(self.cv["width"])
        h = int(self.cv["height"])
        if not reuse_anchor or self._anchor is None:
            self._anchor = _cursor_pos()
        cx, cy = self._anchor
        work, monitor = _monitor_rects(cx, cy)
        edge = _taskbar_edge(work, monitor)
        gap = dpi.px(GAP_DU, self.win)

        if edge == "top":
            y = work.top + gap
        elif edge == "bottom":
            y = work.bottom - h - gap
        else:
            y = _clamp(cy - h // 2, work.top + gap, work.bottom - h - gap)

        if edge == "left":
            x = work.left + gap
        elif edge == "right":
            x = work.right - w - gap
        else:
            x = _clamp(cx - w // 2, work.left + gap, work.right - w - gap)

        self.win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    # --- painting --------------------------------------------------------

    def _px(self, n: float) -> int:
        return dpi.px(n, self.win)

    def _font(self, du: int, bold: bool = False, mono: bool = False):
        return flyout_font(du, self.win, bold=bold, mono=mono)

    def _capsule(self, x, y, w, h, colour) -> None:
        w = max(w, h)
        self.cv.create_oval(x, y, x + h, y + h, fill=colour, outline="")
        self.cv.create_oval(x + w - h, y, x + w, y + h, fill=colour, outline="")
        self.cv.create_rectangle(x + h / 2, y, x + w - h / 2, y + h,
                                 fill=colour, outline="")

    def _ellipsize(self, item: int, limit: float) -> None:
        """Trim one text item until its right edge clears `limit`.

        Estimated first, then adjusted: measuring once per removed character
        cost tens of milliseconds on a long error line, and this repaints
        every second while the panel is open.
        """
        cv = self.cv
        text = cv.itemcget(item, "text")
        if not text or cv.bbox(item)[2] <= limit:
            return

        left = cv.bbox(item)[0]
        overshoot = cv.bbox(item)[2] - limit
        width = max(1.0, cv.bbox(item)[2] - left)
        keep = max(0, int(len(text) * (1.0 - overshoot / width)) - 1)
        text = text[:keep]
        cv.itemconfigure(item, text=text + "…")

        while text and cv.bbox(item)[2] > limit:
            text = text[:-1]
            cv.itemconfigure(item, text=text + "…")

    def paint(self) -> None:
        try:
            self._paint()
        except Exception:
            _log("paint")
            try:
                self.cv.delete("all")
                self.cv.create_text(
                    self._px(PAD_DU), self._px(20), anchor="w",
                    text=t('status.unknown_error'), fill=FLYOUT_CRIT,
                    font=self._font(11, bold=True),
                )
            except tk.TclError:
                pass

    def _paint(self) -> None:
        data = self._data or {}
        claims = list(data.get("claims") or [])
        rows = claims or self._placeholder_rows(data)
        state_text, state_colour, dot_colour = self._state(data)
        footer_text, footer_colour = self._footer(data, rows)

        px = self._px
        cv = self.cv
        cv.delete("all")

        w = px(W_DU)
        pad = px(PAD_DU)
        header = px(HEADER_DU)
        row_h = px(ROW_DU)
        h = header + len(rows) * row_h + (px(FOOT_DU) if footer_text else 0) \
            + px(GAP_BOTTOM_DU)
        cv.configure(width=w, height=h)

        # Header: dot · account · plan ......... state
        cv.create_oval(pad, px(11), pad + px(6), px(17),
                       fill=dot_colour, outline="")
        account = data.get("account") or t('status.no_account')
        acc_item = cv.create_text(pad + px(12), px(14), text=account, anchor="w",
                                  fill=FLYOUT_TEXT, font=self._font(11, bold=True))
        plan_item = None
        plan = data.get("plan")
        if plan:
            plan_item = cv.create_text(
                cv.bbox(acc_item)[2] + px(5), px(14), text="· " + str(plan),
                anchor="w", fill=FLYOUT_DIM, font=self._font(11),
            )
        state_item = None
        if state_text:
            state_item = cv.create_text(w - pad, px(14), text=state_text,
                                        anchor="e", fill=state_colour,
                                        font=self._font(11))

        # Only the account and plan may be trimmed, and the plan goes first —
        # trimming whatever happens to overlap would eat the state word and
        # the dot instead. The plan is dropped rather than shortened: half a
        # plan name ("· Ma…") says less than no plan at all.
        limit = (cv.bbox(state_item)[0] - px(8)) if state_item else (w - pad)
        if plan_item is not None and cv.bbox(plan_item)[2] > limit:
            cv.delete(plan_item)
            plan_item = None
        if cv.bbox(acc_item)[2] > limit:
            self._ellipsize(acc_item, limit)
            if plan_item is not None:
                cv.delete(plan_item)
                plan_item = None
        cv.create_line(pad, px(27), w - pad, px(27), fill=FLYOUT_LINE)

        # One measurement of the widest possible value, so every track starts
        # at the same x and bar lengths can be compared down the column.
        probe = cv.create_text(0, -500, text="100%", anchor="w",
                               font=self._font(21, bold=True, mono=True))
        bbox = cv.bbox(probe)
        val_w = bbox[2] - bbox[0]
        cv.delete(probe)

        bar_x = pad + val_w + px(10)
        bar_w = w - pad - bar_x
        bar_h = px(BAR_DU)
        headline = data.get("headline_key")
        thresholds = data.get("thresholds") or []

        y = header
        for row in rows:
            is_head = row.get("key") == headline
            pct = row.get("pct")
            colour = bar_color(pct) if pct is not None else FLYOUT_DIM

            if is_head:
                cv.create_rectangle(pad - px(9), y + px(3), pad - px(5),
                                    y + px(31), fill=colour, outline="")

            label_item = cv.create_text(
                pad, y + px(7),
                text=row.get("short_label") or row.get("label") or "", anchor="w",
                fill=FLYOUT_TEXT if is_head else FLYOUT_DIM,
                font=self._font(12, bold=True),
            )
            reset_item = cv.create_text(
                w - pad, y + px(7), text=self._reset_text(row, data), anchor="e",
                fill=FLYOUT_DIM, font=self._font(10),
            )
            self._ellipsize(label_item, cv.bbox(reset_item)[0] - px(8))

            value_y = y + px(23)
            cv.create_text(
                pad, value_y, text="—" if pct is None else f"{pct}%", anchor="w",
                fill=colour, font=self._font(21 if is_head else 16,
                                             bold=True, mono=True),
            )

            bar_y = value_y - bar_h / 2
            self._capsule(bar_x, bar_y, bar_w, bar_h, FLYOUT_TRACK)
            if pct:
                fill_w = max(bar_w * min(pct, 100) / 100.0, bar_h)
                self._capsule(bar_x, bar_y, fill_w, bar_h, colour)
            if is_head:
                for threshold in thresholds:
                    if threshold > MAX_TICK_PCT:
                        continue
                    tick_x = bar_x + bar_w * threshold / 100.0
                    # Keep clear of the rounded caps, where a notch reads as a
                    # detached sliver rather than a mark on the bar.
                    if (tick_x - bar_x < bar_h or
                            bar_x + bar_w - tick_x < bar_h):
                        continue
                    cv.create_line(tick_x, bar_y, tick_x, bar_y + bar_h,
                                   fill=FLYOUT_BG, width=px(2))
            y += row_h

        if footer_text:
            cv.create_line(pad, y + px(4), w - pad, y + px(4), fill=FLYOUT_LINE)
            foot_item = cv.create_text(pad, y + px(14), text=footer_text,
                                       anchor="w", fill=footer_colour,
                                       font=self._font(10))
            self._ellipsize(foot_item, w - pad)

    # --- content ---------------------------------------------------------

    def _placeholder_rows(self, data: dict) -> list:
        """Rows to show before the first snapshot, or when one failed."""
        from i18n import claim_label

        keys = data.get("known_keys") or ["five_hour", "seven_day"]
        return [{"key": k, "label": claim_label(k), "pct": None, "reset": None}
                for k in sorted_keys(keys)]

    def _remaining(self, row: dict, data: dict) -> Optional[int]:
        """
        Seconds until this bucket resets, counted down since the poll.

        None once the countdown has run past the reset: the window has rolled
        over and the real figure is whatever the next poll says, not zero.
        """
        reset = row.get("reset")
        if reset is None:
            return None
        fetched = data.get("fetched_at")
        if not fetched:
            return int(reset)
        remaining = int(reset - (time.time() - fetched))
        return remaining if remaining >= 0 else None

    def _reset_text(self, row: dict, data: dict) -> str:
        remaining = self._remaining(row, data)
        return "—" if remaining is None else format_reset(remaining)

    def _age(self, data: dict) -> Optional[float]:
        fetched = data.get("fetched_at")
        return None if not fetched else max(0.0, time.time() - fetched)

    def _state(self, data: dict):
        """(word, word colour, dot colour) for the header.

        `ok` is None before the first snapshot exists, which is not an error —
        reading it as one painted a red "error" over every paused tray.
        """
        age = self._age(data)
        poll = float(data.get("poll_interval") or 180)
        stale = age is not None and age > 2.5 * poll

        if data.get("token_error") or data.get("ok") is False:
            return t('flyout.error'), FLYOUT_CRIT, FLYOUT_CRIT
        if data.get("paused"):
            # A pause explains why the numbers stopped moving; say so, and say
            # it louder once they are old enough to mislead.
            word = t('flyout.paused')
            if stale:
                word = f"{word} · {t('flyout.stale')}"
            return word, FLYOUT_STALE, FLYOUT_STALE
        if age is None:
            return t('flyout.stale'), FLYOUT_DIM, FLYOUT_DIM
        if stale:
            return t('flyout.stale'), FLYOUT_STALE, FLYOUT_STALE
        return "", FLYOUT_DIM, FLYOUT_OK

    def _footer(self, data: dict, rows: list):
        """First rung that applies: error · paused · verdict · hint."""
        error = data.get("token_error") or data.get("error")
        if error:
            # Token errors arrive as multi-line prose. The footer is one line
            # tall and anchored at one point, so a raw newline would paint the
            # rest of the message over the last bucket row and off the panel.
            return " ".join(str(error).split()), FLYOUT_CRIT
        paused = data.get("paused")
        if paused == "battery":
            return t('status.battery_tooltip'), FLYOUT_STALE
        if paused:
            return t('status.paused_tooltip'), FLYOUT_STALE
        if not data.get("claims"):
            return t('status.fetching_tooltip'), FLYOUT_DIM
        verdict = self._verdict(data, rows)
        if verdict:
            return verdict
        return t('flyout.hint'), FLYOUT_DIM

    def _verdict(self, data: dict, rows: list):
        """Whether the headline bucket fills before it resets."""
        key = data.get("headline_key")
        row = next((r for r in rows if r.get("key") == key), None)
        if row is None:
            return None
        info = (data.get("burn") or {}).get(key) or {}
        rate = info.get("rate")
        if rate is None or rate <= 0.05:
            return t('flyout.verdict_idle'), FLYOUT_DIM
        eta = info.get("eta_seconds")
        remaining = self._remaining(row, data)
        if eta is None or remaining is None:
            return t('flyout.verdict_rate', rate=rate), FLYOUT_DIM
        # The ETA was computed at poll time while `remaining` counts down live;
        # comparing them raw turns "full in 2h" into a green "safe" a few
        # minutes later. Age the ETA by the same amount.
        eta = max(0, int(eta - (self._age(data) or 0)))
        if eta >= remaining:
            return (t('flyout.verdict_safe', rate=rate),
                    blend(FLYOUT_OK, FLYOUT_BG, 0.75))
        colour = FLYOUT_STALE if eta >= 1800 else FLYOUT_CRIT
        return t('flyout.verdict_full', eta=format_reset(eta)), colour


# --- registry ------------------------------------------------------------
# One panel per tray icon: dual-icon mode runs two icons in a single process.

_by_icon: dict[int, Flyout] = {}


_building: set[int] = set()


def for_icon(icon, root: tk.Tk, get_data) -> Optional[Flyout]:
    """The panel for this icon, built on first use.

    Returns None while another call is still constructing it: building pumps
    the Tk idle queue, so a second click arriving in that window would
    otherwise build a second panel and orphan the first.
    """
    key = id(icon)
    panel = _by_icon.get(key)
    if panel is None:
        if key in _building:
            return None
        _building.add(key)
        try:
            panel = Flyout(root, get_data)
        finally:
            _building.discard(key)
        _by_icon[key] = panel
    # Rebind every time: the caller's closure carries which bucket this icon
    # currently shows, and that changes from the menu without a restart.
    panel.set_source(get_data)
    return panel


def close_all_except(keep: Optional[Flyout] = None) -> None:
    for panel in list(_by_icon.values()):
        if panel is not keep:
            panel.hide()


def refresh_all() -> None:
    for panel in list(_by_icon.values()):
        try:
            panel.refresh()
        except Exception:
            _log("refresh_all")


def destroy_all() -> None:
    for panel in list(_by_icon.values()):
        panel.destroy()
    _by_icon.clear()
