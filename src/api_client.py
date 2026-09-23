"""
Reads Claude subscription usage.

Two sources, same shape out:

  * `usage_api.fetch_usage_api()` — the OAuth usage endpoint that the /usage
    screen in Claude Code uses. Authoritative, and the only source that breaks
    usage down per model family (Opus / Sonnet / Fable weekly buckets).
  * `fetch_usage()` here — a 1-token Haiku call whose
    `anthropic-ratelimit-unified-*` response headers carry account-level
    windows. Used as a fallback when the endpoint is unavailable.

Both return a `UsageSnapshot` holding a dict of `Claim` buckets keyed by the
canonical names in `claims.py`, so a bucket Anthropic adds later shows up
without a code change.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from claims import Claim, normalize_key, sorted_keys


API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Cheapest, smallest model. The exact string can be updated as new Haikus ship.
DEFAULT_MODEL = "claude-haiku-4-5"

_HEADER_PREFIX = "anthropic-ratelimit-unified-"

# Header suffixes that describe the account as a whole rather than one bucket.
_META_HEADERS = {
    "status",
    "reset",
    "utilization",
    "representative-claim",
    "fallback-percentage",
    "overage-status",
    "overage-disabled-reason",
}

_CLAIM_FIELDS = ("utilization", "reset", "status", "limit")


@dataclass
class UsageSnapshot:
    """One reading of Claude usage limits across every bucket the plan has."""

    claims: dict = field(default_factory=dict)      # key -> Claim
    ok: bool = False
    error: Optional[str] = None
    fetched_at: float = 0.0
    status_code: Optional[int] = None               # HTTP status (None on network error)
    source: Optional[str] = None                    # 'endpoint' | 'headers'
    extra_usage: Optional[dict] = None              # overage credits, when reported
    meta: dict = field(default_factory=dict)        # representative-claim, overage-status, ...

    # --- generic access ---------------------------------------------------

    def claim(self, key: str) -> Optional[Claim]:
        return self.claims.get(normalize_key(key))

    def pct_for(self, key: str) -> Optional[int]:
        c = self.claim(key)
        return c.pct if c else None

    def reset_for(self, key: str) -> Optional[int]:
        c = self.claim(key)
        return c.reset_seconds if c else None

    def status_for(self, key: str) -> Optional[str]:
        c = self.claim(key)
        return c.status if c else None

    def ordered_claims(self) -> list:
        """Claims with data, shortest window first, all-model before per-model."""
        return [self.claims[k] for k in sorted_keys(self.claims) if self.claims[k].has_data]

    def worst_claim(self) -> Optional[Claim]:
        """The bucket closest to its ceiling — the one that stops work first."""
        live = self.ordered_claims()
        if not live:
            return None
        return max(live, key=lambda c: (c.pct, -len(c.key)))

    @property
    def has_data(self) -> bool:
        return any(c.has_data for c in self.claims.values())

    # --- legacy two-metric accessors --------------------------------------
    # Kept so older call sites keep working while the UI moves to claims.

    @property
    def session_pct(self) -> Optional[int]:
        return self.pct_for("five_hour")

    @property
    def weekly_pct(self) -> Optional[int]:
        return self.pct_for("seven_day")

    @property
    def session_reset_seconds(self) -> Optional[int]:
        return self.reset_for("five_hour")

    @property
    def weekly_reset_seconds(self) -> Optional[int]:
        return self.reset_for("seven_day")

    @property
    def session_status(self) -> Optional[str]:
        return self.status_for("five_hour")

    @property
    def weekly_status(self) -> Optional[str]:
        return self.status_for("seven_day")


class APIError(Exception):
    """Raised when the API call itself fails (network, auth, etc.)."""
    pass


def error_snapshot(message: str, status_code: Optional[int] = None,
                   source: Optional[str] = None) -> UsageSnapshot:
    return UsageSnapshot(
        claims={}, ok=False, error=message, fetched_at=time.time(),
        status_code=status_code, source=source,
    )


def pct_from_fraction(raw) -> Optional[int]:
    """Convert a 0..1 fraction (header style) to 0-100. A bare 67 stays 67%."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if 0.0 <= val <= 1.0:
        return max(0, min(100, round(val * 100)))
    return max(0, min(100, round(val)))


def pct_from_percentage(raw) -> Optional[int]:
    """Convert an already-percentage value (endpoint style: 33.0 -> 33)."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, round(val)))


# Back-compat alias for the pre-claims name.
_pct_from_utilization = pct_from_fraction


def seconds_until(raw) -> Optional[int]:
    """
    Parse a reset value into seconds-from-now.

    Accepts ISO 8601 timestamps, Unix epoch seconds, or plain seconds-from-now.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return None

    try:
        ts = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError):
        pass

    try:
        val = float(raw)
        now = time.time()
        if val > now / 2:
            return max(0, int(val - now))
        return max(0, int(val))
    except ValueError:
        return None


_seconds_until_reset = seconds_until


def parse_ratelimit_headers(headers) -> tuple[dict, dict]:
    """
    Turn `anthropic-ratelimit-unified-*` headers into (claims, meta).

    Bucket headers look like `...-unified-<claim>-<field>`, where <claim> is
    `5h`, `7d`, `7d_opus`, ... and <field> is utilization / reset / status.
    """
    raw: dict[str, dict] = {}
    meta: dict[str, str] = {}

    for name, value in headers.items():
        low = name.lower()
        if not low.startswith(_HEADER_PREFIX):
            continue
        rest = low[len(_HEADER_PREFIX):]
        if rest in _META_HEADERS:
            meta[rest] = value
            continue
        if "-" not in rest:
            continue
        claim_raw, _, fld = rest.rpartition("-")
        if fld not in _CLAIM_FIELDS:
            meta[rest] = value
            continue
        raw.setdefault(normalize_key(claim_raw), {})[fld] = value

    parsed = {}
    for key, fields in raw.items():
        parsed[key] = Claim(
            key=key,
            pct=pct_from_fraction(fields.get("utilization")),
            reset_seconds=seconds_until(fields.get("reset")),
            status=fields.get("status"),
        )
    return parsed, meta


def fetch_usage(token: str, model: str = DEFAULT_MODEL,
                timeout: float = 10.0) -> UsageSnapshot:
    """
    Make one minimal API call and read the rate-limit headers off the response.

    Network / auth errors are returned inside the snapshot (ok=False) rather
    than raised, so the tray loop can keep running and display the error.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(API_URL, headers=headers, json=payload)
    except httpx.RequestError as e:
        return error_snapshot(f"Network error: {e}", source="headers")

    # Even on 4xx/5xx the headers we want may still be present, so read them first.
    parsed, meta = parse_ratelimit_headers(resp.headers)
    snapshot = UsageSnapshot(
        claims=parsed,
        ok=resp.is_success or bool(parsed),
        fetched_at=time.time(),
        status_code=resp.status_code,
        source="headers",
        meta=meta,
    )

    if not resp.is_success and not snapshot.has_data:
        try:
            body = resp.json()
            err_msg = body.get("error", {}).get("message", resp.text[:200])
        except Exception:
            err_msg = resp.text[:200] or f"HTTP {resp.status_code}"
        snapshot.ok = False
        snapshot.error = f"API error ({resp.status_code}): {err_msg}"

    return snapshot


def snapshot_has_headers(headers) -> bool:
    """True if at least one ratelimit header is present."""
    return any(k.lower().startswith(_HEADER_PREFIX) for k in headers.keys())


def format_reset(seconds: Optional[int]) -> str:
    """Render seconds-until-reset as a compact string in the active language."""
    from i18n import t

    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return t('unit.s', n=seconds)
    if seconds < 3600:
        return t('unit.m', n=seconds // 60)
    if seconds < 86400:
        h, m = seconds // 3600, (seconds % 3600) // 60
        return f"{t('unit.h', n=h)} {t('unit.m', n=m)}" if m else t('unit.h', n=h)
    d, h = seconds // 86400, (seconds % 86400) // 3600
    return f"{t('unit.d', n=d)} {t('unit.h', n=h)}" if h else t('unit.d', n=d)


if __name__ == "__main__":
    import sys
    from token_reader import read_token, TokenError

    try:
        token = read_token()
    except TokenError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    snap = fetch_usage(token)
    print(f"ok: {snap.ok}  source: {snap.source}  http: {snap.status_code}")
    if snap.error:
        print(f"error: {snap.error}")
    for c in snap.ordered_claims():
        print(f"  {c.label:<18} {c.pct:>3}%   reset in {format_reset(c.reset_seconds)}")
    if snap.meta:
        print(f"  meta: {snap.meta}")
