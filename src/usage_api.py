"""
Authoritative usage readings from the OAuth usage endpoint.

`GET https://api.anthropic.com/api/oauth/usage` is what the /usage screen in
Claude Code itself reads. It is the only source that reports the per-model
weekly buckets (Opus / Sonnet / Fable) that Max plans meter separately, and
unlike the header method it burns no tokens.

Response shape (fields appear only for buckets the plan actually has; a
bucket the account has not touched this cycle comes back as null):

    {
      "five_hour":        {"utilization": 33.0, "resets_at": "2026-...Z"},
      "seven_day":        {"utilization": 13.0, "resets_at": "2026-...Z"},
      "seven_day_opus":   null,
      "seven_day_sonnet": {"utilization": 1.0,  "resets_at": "2026-...Z"},
      "extra_usage":      {"is_enabled": false, "monthly_limit": null, ...}
    }

Utilization here is already a percentage (33.0 means 33%), unlike the
rate-limit headers which report a 0..1 fraction.

The endpoint is undocumented and rate-limits hard by User-Agent: without a
`claude-code/<version>` UA you land in an aggressive bucket and get constant
429s, so the UA is sent and polls stay at/above `MIN_POLL_SECONDS`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from api_client import (
    UsageSnapshot,
    error_snapshot,
    fetch_usage,
    pct_from_percentage,
    seconds_until,
)
from claims import (
    Claim, SUBCAP_FRACTION, known_window, normalize_key, slugify,
)


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

# Used only if the installed CLI version cannot be read from disk.
FALLBACK_CLI_VERSION = "2.1.280"

# The endpoint tolerates a poll every few minutes; below this it starts 429ing.
MIN_POLL_SECONDS = 180

_version_cache: Optional[str] = None


def _npm_package_paths() -> list[Path]:
    """Likely locations of the installed Claude Code package manifest."""
    out: list[Path] = []
    rel = Path("node_modules") / "@anthropic-ai" / "claude-code" / "package.json"

    prefix = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
    if prefix:
        out.append(Path(prefix) / rel)

    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "npm" / rel)

    home = Path.home()
    out += [
        home / ".npm-global" / rel,
        home / ".local" / "share" / "npm" / rel,
        Path("/usr/local/lib") / rel,
    ]

    # npm-global dirs that sit at a drive root on Windows (D:\npm-global\...).
    for drive in ("C:", "D:", "E:"):
        out.append(Path(drive + os.sep) / "npm-global" / rel)

    return out


def detect_cli_version() -> str:
    """Version string of the locally installed Claude Code, best effort."""
    global _version_cache
    if _version_cache:
        return _version_cache
    for path in _npm_package_paths():
        try:
            if path.is_file():
                with open(path, "r", encoding="utf-8") as f:
                    version = json.load(f).get("version")
                if version:
                    _version_cache = str(version)
                    return _version_cache
        except (OSError, json.JSONDecodeError):
            continue
    _version_cache = FALLBACK_CLI_VERSION
    return _version_cache


def user_agent(version: Optional[str] = None) -> str:
    return f"claude-code/{version or detect_cli_version()} (external, cli)"


def _apply_subcaps(parsed: dict, plan: Optional[str]) -> None:
    """
    Tag buckets that may only consume part of their parent window.

    On Max, Fable models draw from the weekly pool but are capped at 50% of
    it, so 100% of the Fable bucket is not 100% of the week.
    """
    if not plan or not plan.lower().startswith("max"):
        return
    for key, fraction in SUBCAP_FRACTION.items():
        if key in parsed:
            parsed[key].cap_fraction = fraction


_LIMIT_KIND_WINDOWS = {
    "session": "five_hour",
    "five_hour": "five_hour",
    "weekly_all": "seven_day",
    "weekly": "seven_day",
    "weekly_scoped": "seven_day",
    "monthly": "thirty_day",
}


def _claims_from_limits(limits: list) -> dict:
    """
    Build buckets from the `limits` array.

    This is the list the /usage screen itself renders: each entry names its
    window (`kind`), its percentage, its reset, and — for a scoped weekly —
    the model or surface it covers, with a display name the server chose.
    """
    parsed: dict[str, Claim] = {}
    for entry in limits or []:
        if not isinstance(entry, dict):
            continue
        window = _LIMIT_KIND_WINDOWS.get(entry.get("kind") or "")
        if not window:
            continue

        scope = entry.get("scope") or {}
        display_name = None
        for part in ("model", "surface"):
            info = scope.get(part) if isinstance(scope, dict) else None
            if isinstance(info, dict) and info.get("display_name"):
                display_name = info["display_name"]
                break

        key = window
        if display_name:
            key = f"{window}_{slugify(display_name)}"
        if key in parsed:
            continue

        parsed[key] = Claim(
            key=key,
            pct=pct_from_percentage(entry.get("percent")),
            reset_seconds=seconds_until(entry.get("resets_at")),
            status=entry.get("severity"),
            display_name=display_name,
            active=entry.get("is_active"),
        )
    return parsed


def _claims_from_top_level(data: dict) -> tuple[dict, Optional[dict]]:
    """Older shape: one top-level object per bucket, keyed by window name."""
    parsed: dict[str, Claim] = {}
    extra: Optional[dict] = None

    for raw_key, value in (data or {}).items():
        if raw_key == "extra_usage":
            extra = value if isinstance(value, dict) else None
            continue
        if not isinstance(value, dict) or "utilization" not in value:
            # null means the plan has the bucket but nothing has used it.
            continue
        key = normalize_key(raw_key)
        # Internal codename buckets (nimbus_quill, copper_kite, ...) name no
        # window and carry no reset — not something to put in a tray.
        if not known_window(key):
            continue
        parsed[key] = Claim(
            key=key,
            pct=pct_from_percentage(value.get("utilization")),
            reset_seconds=seconds_until(value.get("resets_at") or value.get("reset")),
            status=value.get("status") or value.get("locked_reason"),
        )
    return parsed, extra


def parse_usage_payload(data: dict, plan: Optional[str] = None) -> tuple[dict, Optional[dict]]:
    """Turn the endpoint JSON into (claims, extra_usage)."""
    data = data or {}
    parsed, extra = _claims_from_top_level(data)

    # `limits` is the curated view and the only place scoped weekly buckets
    # get a name, so it wins where the two overlap.
    from_limits = _claims_from_limits(data.get("limits"))
    for key, claim in from_limits.items():
        existing = parsed.get(key)
        if existing is not None and claim.reset_seconds is None:
            claim.reset_seconds = existing.reset_seconds
        parsed[key] = claim

    _apply_subcaps(parsed, plan)
    return parsed, extra


def _dump_payload(data: dict) -> None:
    """
    Keep the last raw endpoint response next to the settings file.

    Anthropic adds and renames buckets without notice (model-family windows
    have shipped under codenames), and the file makes it obvious what the
    account actually returns. It holds usage percentages only — no token.
    """
    try:
        from settings import SETTINGS_DIR
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = SETTINGS_DIR / "last_usage_payload.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "payload": data}, f,
                      indent=2, ensure_ascii=False)
    except (OSError, TypeError, ValueError):
        pass


def _meta_from_payload(data: dict) -> dict:
    """Side facts worth showing: where the week went, and credit spend."""
    meta: dict = {}
    breakdown = (data or {}).get("seven_day_breakdown") or {}
    rows = [
        {"label": row.get("display_name") or row.get("key"),
         "pct": pct_from_percentage(row.get("percent"))}
        for row in breakdown.get("rows") or []
        if isinstance(row, dict) and row.get("percent")
    ]
    if rows:
        meta["weekly_breakdown"] = rows
    spend = (data or {}).get("spend") or {}
    if spend.get("enabled"):
        meta["spend"] = spend
    return meta


def fetch_usage_api(token: str, timeout: float = 10.0,
                    plan: Optional[str] = None) -> UsageSnapshot:
    """Read every bucket from the OAuth usage endpoint."""
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "content-type": "application/json",
        "user-agent": user_agent(),
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(USAGE_URL, headers=headers)
    except httpx.RequestError as e:
        return error_snapshot(f"Network error: {e}", source="endpoint")

    if not resp.is_success:
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message") or resp.text[:200]
        except Exception:
            msg = resp.text[:200] or f"HTTP {resp.status_code}"
        return error_snapshot(
            f"Usage endpoint error ({resp.status_code}): {msg}",
            status_code=resp.status_code, source="endpoint",
        )

    try:
        data = resp.json()
    except ValueError:
        return error_snapshot("Usage endpoint returned non-JSON",
                              status_code=resp.status_code, source="endpoint")

    _dump_payload(data)
    parsed, extra = parse_usage_payload(data, plan)
    snap = UsageSnapshot(
        claims=parsed,
        ok=bool(parsed),
        fetched_at=time.time(),
        status_code=resp.status_code,
        source="endpoint",
        extra_usage=extra,
        meta=_meta_from_payload(data),
    )
    if not parsed:
        snap.error = "Usage endpoint returned no usage windows"
    return snap


def fetch_best(token: str, plan: Optional[str] = None, timeout: float = 10.0,
               allow_header_fallback: bool = True,
               model: Optional[str] = None) -> UsageSnapshot:
    """
    Endpoint first (per-model buckets, no token spend), header method second.

    The fallback keeps the tray alive if Anthropic changes or gates the
    endpoint; it just cannot see per-model buckets.
    """
    snap = fetch_usage_api(token, timeout=timeout, plan=plan)
    if snap.ok and snap.has_data:
        return snap
    if not allow_header_fallback:
        return snap

    fallback = (fetch_usage(token, model=model, timeout=timeout) if model
                else fetch_usage(token, timeout=timeout))
    if fallback.ok and fallback.has_data:
        if snap.error:
            fallback.error = None
            fallback.meta = dict(fallback.meta)
            fallback.meta["endpoint_error"] = snap.error
        return fallback
    return snap if snap.error else fallback


if __name__ == "__main__":
    import sys
    from api_client import format_reset
    from token_reader import TokenError

    try:
        import accounts
        account = accounts.active_account()
        creds = accounts.get_credentials(account)
        print(f"account: {account.get('name')}")
    except TokenError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"plan: {creds.get('plan')}   user-agent: {user_agent()}")
    snap = fetch_usage_api(creds["token"], plan=creds.get("plan"))
    print(f"ok: {snap.ok}  source: {snap.source}  http: {snap.status_code}")
    if snap.error:
        print(f"error: {snap.error}")
    for c in snap.ordered_claims():
        cap = f"   (cap {int(c.cap_fraction * 100)}% of window)" if c.cap_fraction else ""
        print(f"  {c.key:<20} {c.label:<16} {c.pct:>3}%   reset in "
              f"{format_reset(c.reset_seconds)}{cap}")
    if snap.extra_usage:
        print(f"  extra_usage: {snap.extra_usage}")
