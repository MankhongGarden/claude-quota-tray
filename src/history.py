"""
Snapshot history persisted to a small SQLite database.

Used for:
  - the 'Show history' chart window
  - burn-rate / ETA calculation

Rows keep the original session/weekly columns so older readers stay valid,
and carry a `claims_json` blob with every bucket in the snapshot (including
the per-model weekly ones) so new buckets need no schema change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from claims import normalize_key
from settings import SETTINGS_DIR


DB_PATH = SETTINGS_DIR / "history.db"

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

# Buckets the legacy columns mirror.
SESSION_KEY = "five_hour"
WEEKLY_KEY = "seven_day"


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                ts REAL NOT NULL,
                account_id TEXT NOT NULL,
                session_pct INTEGER,
                weekly_pct INTEGER,
                session_reset INTEGER,
                weekly_reset INTEGER
            )
            """
        )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)"
        )
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "claims_json" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN claims_json TEXT")


def _claims_blob(snapshot) -> Optional[str]:
    claims = getattr(snapshot, "claims", None) or {}
    payload = {
        key: {"pct": c.pct, "reset": c.reset_seconds}
        for key, c in claims.items()
        if c.pct is not None
    }
    if not payload:
        return None
    try:
        return json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        return None


def record(account_id: str, snapshot) -> None:
    """Persist a UsageSnapshot. Errors are swallowed (history is best-effort)."""
    if not getattr(snapshot, "ok", False):
        return
    blob = _claims_blob(snapshot)
    if snapshot.session_pct is None and snapshot.weekly_pct is None and blob is None:
        return
    try:
        with _lock:
            conn = _connect()
            conn.execute(
                "INSERT INTO snapshots "
                "(ts, account_id, session_pct, weekly_pct, session_reset, "
                "weekly_reset, claims_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.fetched_at or time.time(),
                    account_id,
                    snapshot.session_pct,
                    snapshot.weekly_pct,
                    snapshot.session_reset_seconds,
                    snapshot.weekly_reset_seconds,
                    blob,
                ),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def recent(hours: float = 24, account_id: Optional[str] = None) -> list[tuple]:
    """Return (ts, session_pct, weekly_pct) rows from the last N hours."""
    cutoff = time.time() - hours * 3600
    try:
        with _lock:
            conn = _connect()
            if account_id:
                cur = conn.execute(
                    "SELECT ts, session_pct, weekly_pct FROM snapshots "
                    "WHERE ts >= ? AND account_id = ? ORDER BY ts",
                    (cutoff, account_id),
                )
            else:
                cur = conn.execute(
                    "SELECT ts, session_pct, weekly_pct FROM snapshots "
                    "WHERE ts >= ? ORDER BY ts",
                    (cutoff,),
                )
            return cur.fetchall()
    except sqlite3.Error:
        return []


def recent_claims(hours: float = 24,
                  account_id: Optional[str] = None) -> list[tuple[float, dict]]:
    """
    Return (ts, {claim_key: pct}) rows from the last N hours.

    Rows written before the claims column existed fall back to their
    session/weekly values, so history stays continuous across the upgrade.
    """
    cutoff = time.time() - hours * 3600
    try:
        with _lock:
            conn = _connect()
            sql = ("SELECT ts, session_pct, weekly_pct, claims_json FROM snapshots "
                   "WHERE ts >= ?")
            params: tuple = (cutoff,)
            if account_id:
                sql += " AND account_id = ?"
                params = (cutoff, account_id)
            rows = conn.execute(sql + " ORDER BY ts", params).fetchall()
    except sqlite3.Error:
        return []

    out: list[tuple[float, dict]] = []
    for ts, session_pct, weekly_pct, blob in rows:
        values: dict[str, int] = {}
        if blob:
            try:
                for key, payload in json.loads(blob).items():
                    pct = payload.get("pct") if isinstance(payload, dict) else payload
                    if pct is not None:
                        values[normalize_key(key)] = int(pct)
            except (ValueError, TypeError, AttributeError):
                values = {}
        if SESSION_KEY not in values and session_pct is not None:
            values[SESSION_KEY] = int(session_pct)
        if WEEKLY_KEY not in values and weekly_pct is not None:
            values[WEEKLY_KEY] = int(weekly_pct)
        if values:
            out.append((ts, values))
    return out


def series(key: str, hours: float = 24,
           account_id: Optional[str] = None) -> list[tuple[float, int]]:
    """(ts, pct) points for one bucket, oldest first."""
    key = normalize_key(key)
    return [(ts, vals[key]) for ts, vals in recent_claims(hours, account_id)
            if key in vals]


def known_keys(hours: float = 24, account_id: Optional[str] = None) -> list[str]:
    """Bucket keys seen in the recent history window."""
    seen: set[str] = set()
    for _ts, vals in recent_claims(hours, account_id):
        seen.update(vals)
    return sorted(seen)


def burn_rate(window_minutes: float = 60,
              account_id: Optional[str] = None) -> dict:
    """
    Usage growth rate (% per hour) and ETA-to-full for every bucket in the
    recent window. Keys are canonical claim names; "session" and "weekly"
    stay as aliases for older call sites.

    Rate is None when there are fewer than two points or usage is not growing.
    """
    rows = recent_claims(window_minutes / 60.0, account_id)
    out: dict[str, dict] = {}

    keys: set[str] = set()
    for _ts, vals in rows:
        keys.update(vals)

    for key in keys:
        points = [(ts, vals[key]) for ts, vals in rows if key in vals]
        out[key] = _rate_for(points)

    out.setdefault(SESSION_KEY, _empty_rate())
    out.setdefault(WEEKLY_KEY, _empty_rate())
    out["session"] = out[SESSION_KEY]
    out["weekly"] = out[WEEKLY_KEY]
    return out


def _rate_for(points: list[tuple[float, int]]) -> dict:
    if len(points) < 2:
        return _empty_rate()

    # A percentage only falls when the window resets. Measuring from before
    # that drop to now nets out to roughly zero and reports "idle" for an hour
    # after every reset, so only the run since the last drop counts.
    start = 0
    for i in range(1, len(points)):
        if points[i][1] < points[i - 1][1]:
            start = i
    points = points[start:]
    if len(points) < 2:
        return _empty_rate()

    first, last = points[0], points[-1]
    dt_hours = (last[0] - first[0]) / 3600.0
    if dt_hours <= 0:
        return _empty_rate()
    rate = (last[1] - first[1]) / dt_hours  # pct/hour
    eta = None
    current = last[1]
    if rate > 0.01 and current < 100:
        eta = int(((100 - current) / rate) * 3600)
    return {"rate": rate, "eta_seconds": eta}


def _empty_rate() -> dict:
    return {"rate": None, "eta_seconds": None}


def prune(retention_days: int) -> None:
    """Delete rows older than retention_days."""
    if retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    try:
        with _lock:
            conn = _connect()
            conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            conn.commit()
    except sqlite3.Error:
        pass
