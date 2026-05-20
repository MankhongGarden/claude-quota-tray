"""
Today's spend lookup via the `ccusage` CLI.

ccusage parses the Claude Code JSONL session logs and reports cost per day.
We shell out to it periodically (not every poll — it walks files) and cache
the result so the tooltip/menu can show "Today: $X.XX" alongside the %.

If ccusage isn't installed or the call fails we just return None and the
caller drops the line — the rest of the tray keeps working.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional


_lock = threading.Lock()
_refresh_inflight = threading.Event()
_cache: dict = {
    "today_usd": None,   # float or None
    "fetched_at": 0.0,   # epoch
    "error": None,       # last error string, for debugging
}

_REFRESH_SECONDS = 300  # 5 min; ccusage walks JSONL so don't hammer it


def today_usd() -> Optional[float]:
    """Return today's spend in USD, or None if unknown.

    Returns the cached value immediately. If the cache is stale, kicks off a
    background refresh — the next call returns the updated value. ccusage
    can take 30-60s to walk the JSONL session logs, so we never block the
    poll loop on it.
    """
    now = time.time()
    if (now - _cache["fetched_at"] >= _REFRESH_SECONDS
            and not _refresh_inflight.is_set()):
        _refresh_inflight.set()
        threading.Thread(target=_refresh_then_clear, daemon=True).start()
    return _cache["today_usd"]


def _refresh_then_clear() -> None:
    try:
        _refresh()
    finally:
        _refresh_inflight.clear()


def _refresh() -> None:
    with _lock:
        _cache["fetched_at"] = time.time()
        try:
            today = datetime.now().strftime("%Y%m%d")
            cmd = (f'ccusage daily --since {today} '
                   '--json --offline --no-color')
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                shell=True,  # ccusage on Windows is a .cmd shim
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            _cache["error"] = f"ccusage call failed: {e}"
            _cache["today_usd"] = None
            return
        if proc.returncode != 0:
            _cache["error"] = f"ccusage exit {proc.returncode}: {proc.stderr[:200]}"
            _cache["today_usd"] = None
            return
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError) as e:
            _cache["error"] = f"ccusage JSON parse: {e}"
            _cache["today_usd"] = None
            return
        # ccusage daily output shape: {"daily": [{"date": "2026-05-20", "totalCost": ...}, ...]}
        daily = data.get("daily") if isinstance(data, dict) else None
        if not daily:
            _cache["today_usd"] = 0.0
            _cache["error"] = None
            return
        today_iso = datetime.now().strftime("%Y-%m-%d")
        today_row = next((r for r in daily if r.get("date") == today_iso), None)
        if today_row is None:
            _cache["today_usd"] = 0.0
        else:
            _cache["today_usd"] = float(today_row.get("totalCost") or 0.0)
        _cache["error"] = None


def format_cost(usd: Optional[float]) -> str:
    if usd is None:
        return "—"
    if usd < 0.01:
        return "$0.00"
    return f"${usd:.2f}"
