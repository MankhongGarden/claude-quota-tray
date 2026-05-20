"""
Active-window attribution: when the 5h limit goes up between polls, credit
the delta to whichever window happened to be focused when the poll fired.

Stores deltas in a tiny SQLite table inside the same data dir as settings,
so it survives restarts and works alongside aggregate/dual-icon modes.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional

from settings import SETTINGS_DIR


_DB_PATH = SETTINGS_DIR / "attribution.db"
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_last_session_pct: Optional[int] = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS window_usage (
                ts REAL NOT NULL,
                title TEXT NOT NULL,
                delta_pct INTEGER NOT NULL
            )
            """
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_window_usage_ts ON window_usage(ts)")
        _conn.commit()
    return _conn


def _foreground_title() -> str:
    """Title of the currently focused window (Win32). Empty string on
    failure or non-Windows hosts."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = u32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        u32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value[:60]
    except Exception:
        return ""


def _normalize_title(title: str) -> str:
    """Collapse multi-segment titles like 'main.py - CheckMate - VS Code' into
    a more glanceable label. Keeps the longest meaningful segment."""
    for sep in (" — ", " - ", " | "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            if len(parts) >= 2:
                # Drop the trailing app name (e.g. "Visual Studio Code", "Google Chrome"),
                # keep the next-to-last segment which usually names the workspace.
                return parts[-2][:40]
    return title[:40]


def record(snapshot) -> None:
    """Called from the poll loop. If session_pct increased, credit the delta
    to whichever window was focused at that moment."""
    global _last_session_pct
    cur = getattr(snapshot, "session_pct", None)
    if cur is None:
        _last_session_pct = None
        return
    if _last_session_pct is None:
        _last_session_pct = cur
        return
    delta = cur - _last_session_pct
    _last_session_pct = cur
    if delta <= 0:
        return
    title = _normalize_title(_foreground_title())
    if not title:
        return
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                "INSERT INTO window_usage VALUES (?, ?, ?)",
                (time.time(), title, int(delta)),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def top_recent(hours: float = 1.0, limit: int = 3) -> list[tuple[str, int]]:
    """Return [(window_title, delta_pct_total)] for the last N hours,
    largest first."""
    cutoff = time.time() - hours * 3600
    try:
        with _lock:
            conn = _connect()
            cur = conn.execute(
                "SELECT title, SUM(delta_pct) FROM window_usage "
                "WHERE ts >= ? GROUP BY title ORDER BY 2 DESC LIMIT ?",
                (cutoff, limit),
            )
            return [(r[0], int(r[1] or 0)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def prune(retention_days: int = 7) -> None:
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    try:
        with _lock:
            conn = _connect()
            conn.execute("DELETE FROM window_usage WHERE ts < ?", (cutoff,))
            conn.commit()
    except sqlite3.Error:
        pass
