"""
Quota claims — one usage bucket each.

Claude meters a subscription with several independent buckets ("claims"):
a rolling 5-hour window and a 7-day window that cover every model, plus
per-model-family 7-day windows (Opus, Sonnet, Fable, ...). Which ones exist
depends on the plan and on which models the account has actually used, so
nothing here hardcodes the full set: unknown keys are carried through and
labelled generically.

Canonical key names follow the OAuth usage endpoint:
    five_hour, seven_day, seven_day_opus, seven_day_sonnet, seven_day_fable
The rate-limit response headers use a shorter spelling (5h, 7d, 7d_opus)
which `normalize_key` folds into the canonical form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Legacy two-metric vocabulary used by older settings files and menus.
LEGACY_ALIASES = {
    "session": "five_hour",
    "weekly": "seven_day",
}

# Header spellings -> canonical window prefix.
_HEADER_WINDOWS = {
    "5h": "five_hour",
    "7d": "seven_day",
    "1h": "one_hour",
    "1d": "one_day",
    "30d": "thirty_day",
}

_WINDOW_LABELS = {
    "five_hour": "5h",
    "seven_day": "Weekly",
    "one_hour": "1h",
    "one_day": "Daily",
    "thirty_day": "Monthly",
}

_MODEL_LABELS = {
    "opus": "Opus",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "fable": "Fable",
}

# Plan rules that cap a bucket below its parent window. On Max, Fable models
# draw from the weekly pool but may take at most 50% of it, so a full Fable
# bucket is not a full week.
SUBCAP_FRACTION = {
    "seven_day_fable": 0.5,
}

# Sort weight per window so tooltips read shortest-window-first.
_WINDOW_ORDER = {
    "one_hour": 0,
    "five_hour": 1,
    "one_day": 2,
    "seven_day": 3,
    "thirty_day": 4,
}


def normalize_key(raw: str) -> str:
    """Fold header/legacy spellings into a canonical claim key."""
    key = (raw or "").strip().lower().replace("-", "_")
    if key in LEGACY_ALIASES:
        return LEGACY_ALIASES[key]
    for header, window in _HEADER_WINDOWS.items():
        if key == header:
            return window
        if key.startswith(header + "_"):
            return window + "_" + key[len(header) + 1:]
    return key


def split_key(key: str) -> tuple[str, Optional[str]]:
    """Return (window, model) for a canonical key; model is None for all-model buckets."""
    for window in _WINDOW_LABELS:
        if key == window:
            return window, None
        if key.startswith(window + "_"):
            return window, key[len(window) + 1:]
    return key, None


def model_label(model: str) -> str:
    """Display name for a model family."""
    return _MODEL_LABELS.get(model, model.replace("_", " ").title())


def known_window(key: str) -> bool:
    """
    True for keys that name a real usage window.

    The endpoint also returns internal codename buckets (`nimbus_quill`,
    `copper_kite`, ...) that carry no window and no reset; those are not
    something to show in a tray.
    """
    return split_key(key)[0] in _WINDOW_LABELS


def slugify(name: str) -> str:
    """'Claude Code' -> 'claude_code', for building a key from a display name."""
    out = []
    for ch in (name or "").strip().lower():
        out.append(ch if ch.isalnum() else "_")
    return "_".join(part for part in "".join(out).split("_") if part)


def default_label(key: str, display_name: Optional[str] = None) -> str:
    """Human label such as '5h', 'Weekly' or 'Weekly · Opus'."""
    window, model = split_key(key)
    window_label = _WINDOW_LABELS.get(window, window.replace("_", " ").title())
    suffix = display_name or (model_label(model) if model else None)
    return f"{window_label} · {suffix}" if suffix else window_label


def sort_index(key: str) -> tuple:
    """Sort key: by window length, all-model bucket before per-model ones."""
    window, model = split_key(key)
    return (_WINDOW_ORDER.get(window, 90), 0 if model is None else 1, model or "")


def sorted_keys(keys) -> list[str]:
    return sorted(keys, key=sort_index)


@dataclass
class Claim:
    """One bucket's current state."""
    key: str
    pct: Optional[int] = None
    reset_seconds: Optional[int] = None
    status: Optional[str] = None
    # Share of the parent window this bucket may consume (Fable is capped at
    # 50% of the weekly limit on Max). None when the plan sets no sub-cap.
    cap_fraction: Optional[float] = None
    # Name the server gave this bucket ("Fable", "Cowork", ...). Preferred
    # over any local mapping, since Anthropic ships buckets we know nothing
    # about and renames them.
    display_name: Optional[str] = None
    # Whether this window is the one currently metering, per the server.
    active: Optional[bool] = None

    @property
    def window(self) -> str:
        return split_key(self.key)[0]

    @property
    def model(self) -> Optional[str]:
        return split_key(self.key)[1]

    @property
    def label(self) -> str:
        return default_label(self.key, self.display_name)

    @property
    def has_data(self) -> bool:
        return self.pct is not None
