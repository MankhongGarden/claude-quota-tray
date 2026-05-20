"""
User-editable settings persisted to ~/.claude-quota-tray/settings.json.

Separate from config.py (which holds environment-driven defaults). These
values can change at runtime via the tray menu and survive across restarts.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict


_override = os.environ.get("CQT_DATA_DIR")
SETTINGS_DIR = Path(_override).expanduser() if _override else Path.home() / ".claude-quota-tray"
SETTINGS_PATH = SETTINGS_DIR / "settings.json"

_lock = threading.RLock()
_cache: Dict[str, Any] | None = None


def _defaults() -> Dict[str, Any]:
    return {
        "accounts": [
            {
                "id": str(uuid.uuid4()),
                "name": "Default",
                "mode": "auto",  # "auto" | "file"
                "path": None,
            }
        ],
        "active_account_id": None,  # falls back to accounts[0]
        "thresholds": [80, 95],
        "sound_alerts": True,
        "schedule": {
            "enabled": False,
            "start_hour": 9,
            "end_hour": 18,
            "days": [0, 1, 2, 3, 4],  # Mon-Fri (0=Mon)
        },
        "theme": "auto",  # "auto" | "light" | "dark"
        "icon_style": "frame",  # "frame" | "solid" | "donut" | "bar"
        "headline_metric": "session",  # "session" (5h) | "weekly" (7d) — which figure the tray icon shows
        "poll_interval_seconds": 60,
        "history_retention_days": 7,
        "language": None,  # None → auto-detect from locale on first read
        "pause_on_battery": True,  # skip polls while laptop runs on battery
        "eta_alert_minutes": 60,  # one-shot alert when projected to hit 100% within N min
        "show_cost": True,  # show today's $ from ccusage in popup + menu
        "show_sparkline": True,  # render Unicode trend bar in tooltip
        "attribute_active_window": False,  # sample Win32 foreground window per poll
    }


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in missing keys from defaults."""
    defaults = _defaults()
    for key, val in defaults.items():
        data.setdefault(key, val)
    sched = data.get("schedule") or {}
    for key, val in defaults["schedule"].items():
        sched.setdefault(key, val)
    data["schedule"] = sched

    if not data.get("accounts"):
        data["accounts"] = defaults["accounts"]
    return data


def load() -> Dict[str, Any]:
    """Load settings from disk (cached). Creates the file on first run."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data = _migrate(data)
            except (OSError, json.JSONDecodeError):
                data = _defaults()
        else:
            data = _defaults()
        _cache = data
        save(data)
        return _cache


def save(data: Dict[str, Any] | None = None) -> None:
    """Persist settings to disk."""
    global _cache
    with _lock:
        if data is None:
            data = _cache
        if data is None:
            return
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(SETTINGS_PATH)
        _cache = data


def update(**kwargs: Any) -> Dict[str, Any]:
    """Shallow-update top-level keys and persist."""
    with _lock:
        data = load()
        for key, val in kwargs.items():
            data[key] = val
        save(data)
        return data


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)
