"""
Claude Quota Tray — entry point.

A small system-tray app for Windows (and macOS/Linux) that polls Claude
usage and displays one bucket as a coloured badge in the tray. Hover the
icon for every bucket the plan has — the 5-hour and weekly windows plus the
per-model weekly ones (Opus / Sonnet / Fable) — right-click for actions.
"""

import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import dpi

# Before anything can create a window: an unaware process draws on a
# virtualised 96-DPI desktop and is then stretched, which is what made the
# panels look soft. This also lifts the tray icon slot from 16px to 20px.
dpi.enable()

import pystray

import config
import cost
import notifications
import settings as user_settings
import history
import sound
import theme as theme_mod
import accounts
import flyout
import history_window
import settings_dialogs
import tk_host
from i18n import LANGUAGES, claim_label, set_language, t
from bar_widget import color_emoji, unicode_bar
from api_client import fetch_usage, format_reset, UsageSnapshot
from claims import normalize_key, sorted_keys, model_label, split_key
from usage_api import MIN_POLL_SECONDS, fetch_best
from icon_renderer import render_icon
from token_reader import TokenError


REPO_URL = "https://github.com/kpcrmv4/claude-quota-tray"
CONSOLE_USAGE_URL = "https://console.anthropic.com/settings/usage"
CONSOLE_LIMITS_URL = "https://console.anthropic.com/settings/limits"


# --- State held across the lifetime of the tray icon ----------------------

class AppState:
    def __init__(self):
        self.token: Optional[str] = None
        self.token_error: Optional[str] = None
        self.snapshot: Optional[UsageSnapshot] = None
        self.burn: dict = {}
        self.force_refresh = threading.Event()
        self.stop = threading.Event()
        # Keyed by claim key; buckets appear as the account uses them.
        self.fired_thresholds: dict = defaultdict(set)
        self.last_prune = 0.0
        # time.monotonic() of the last poll-loop iteration. The heartbeat
        # thread keys off this so a wedged poll loop (alive process, frozen
        # polling) lets the heartbeat go stale and the watchdog can restart us.
        # Monotonic, not wall clock: sleeping the laptop advances the wall
        # clock by the whole nap and would look exactly like a wedge.
        # 0.0 until main() baselines it just before the loop spins.
        self.last_loop_tick = 0.0
        self.active_account: Optional[dict] = None
        self.plan: Optional[str] = None
        self.paused_by_schedule = False
        self.paused_by_battery = False
        self.fired_eta: dict = defaultdict(bool)

    @property
    def headline_pct(self) -> Optional[int]:
        return self.headline_pct_for(user_settings.get("headline_metric", "session"))

    def headline_claim_for(self, metric: str):
        """The bucket a given icon shows.

        `metric` is a claim key ("five_hour", "seven_day_opus", ...), one of
        the legacy names ("session" / "weekly"), or "auto" for whichever
        bucket is closest to full. Falls back to any bucket with data so the
        icon never goes blank just because one window is missing.
        """
        snap = self.snapshot
        if not snap or not snap.has_data:
            return None
        if metric == "auto":
            return snap.worst_claim()
        claim = snap.claim(metric)
        if claim and claim.has_data:
            return claim
        for fallback in ("five_hour", "seven_day"):
            claim = snap.claim(fallback)
            if claim and claim.has_data:
                return claim
        return snap.worst_claim()

    def headline_pct_for(self, metric: str) -> Optional[int]:
        """Number shown on the tray icon for a specific bucket."""
        claim = self.headline_claim_for(metric)
        return claim.pct if claim else None

    @property
    def is_error(self) -> bool:
        if self.token_error:
            return True
        if self.snapshot and not self.snapshot.ok:
            return True
        return False


state = AppState()

# All registered tray icons (1 in single-icon mode, 2 in dual-icon mode).
# Refreshed in lockstep after each successful poll.
ICONS: list = []


def _metric_for(icon) -> str:
    """Bucket this icon shows: per-icon override, else the saved setting.

    Returns a canonical claim key, or "auto" for the busiest bucket.
    """
    override = getattr(icon, "metric_override", None)
    return _normalize_metric(override or user_settings.get("headline_metric", "session"))


def _normalize_metric(metric) -> str:
    if metric == "auto":
        return "auto"
    return normalize_key(metric or "five_hour")


def _available_metrics() -> list[str]:
    """Bucket keys the user can point an icon at, busiest-first order aside."""
    keys = set()
    if state.snapshot:
        keys.update(k for k, c in state.snapshot.claims.items() if c.has_data)
    keys.update({"five_hour", "seven_day"})
    return sorted_keys(keys)


def _label(claim) -> str:
    """Full label for a bucket, using the name the server gave its scope."""
    return claim_label(claim.key, getattr(claim, "display_name", None))


def _compact_label(claim) -> str:
    """Short label for tooltips: scope name alone for scoped buckets."""
    display = getattr(claim, "display_name", None)
    if display:
        return display
    _window, model = split_key(normalize_key(claim.key))
    return model_label(model) if model else claim_label(claim.key)


# --- Helpers --------------------------------------------------------------

def _current_theme() -> str:
    return theme_mod.effective_theme(user_settings.get("theme", "auto"))


def _current_icon_style() -> str:
    from icon_renderer import STYLES
    style = user_settings.get("icon_style", "frame")
    return style if style in STYLES else "frame"


def _thresholds() -> list[int]:
    val = user_settings.get("thresholds", config.NOTIFY_THRESHOLDS)
    return sorted({int(t) for t in val if isinstance(t, (int, float))})


def _within_schedule() -> bool:
    sched = user_settings.get("schedule", {}) or {}
    if not sched.get("enabled"):
        return True
    now = datetime.now()
    if now.weekday() not in sched.get("days", []):
        return False
    h = now.hour + now.minute / 60.0
    start = float(sched.get("start_hour", 0))
    end = float(sched.get("end_hour", 24))
    if start <= end:
        return start <= h < end
    # Wrap past midnight (e.g., 22-6)
    return h >= start or h < end


def _on_battery() -> bool:
    """True if the device is running on battery (not plugged in).
    Returns False on desktops or when psutil/the battery API is unavailable."""
    if not bool(user_settings.get("pause_on_battery", True)):
        return False
    try:
        import psutil
        bat = psutil.sensors_battery()
        if bat is None:
            return False
        return not bat.power_plugged
    except Exception:
        return False


def _load_active_token() -> None:
    """Resolve the active account, token, and plan info."""
    try:
        acct = accounts.active_account()
        state.active_account = acct
        creds = accounts.get_credentials(acct)
        state.token = creds["token"]
        state.plan = creds.get("plan")
        state.token_error = None
    except TokenError as e:
        state.active_account = None
        state.token = None
        state.plan = None
        state.token_error = str(e)


# --- Background poller ----------------------------------------------------

def poll_loop(icon: pystray.Icon):
    """Outer wrapper that auto-restarts the inner loop on uncaught errors."""
    while not state.stop.is_set():
        try:
            _poll_loop_inner(icon)
            return  # Clean exit (stop set inside inner)
        except Exception:
            _log_action_error("poll_loop")
            # Brief pause so we don't hot-loop on a persistent failure
            state.force_refresh.clear()
            state.force_refresh.wait(timeout=15)


def _poll_loop_inner(icon: pystray.Icon):
    _load_active_token()
    _refresh_all_icons()
    if state.token_error and state.token is None:
        # Without a token, sit idle but keep checking — user can configure
        # an account from the menu and we'll pick it up on next iteration.
        while not state.stop.is_set():
            state.last_loop_tick = time.monotonic()
            state.force_refresh.clear()
            state.force_refresh.wait(timeout=30)
            _load_active_token()
            _refresh_all_icons()
            if state.token:
                break
        if state.stop.is_set():
            return

    time.sleep(config.INITIAL_DELAY_SECONDS)

    while not state.stop.is_set():
        state.last_loop_tick = time.monotonic()
        if not _within_schedule():
            state.paused_by_schedule = True
            state.paused_by_battery = False
            _refresh_all_icons()
        elif _on_battery():
            state.paused_by_schedule = False
            state.paused_by_battery = True
            _refresh_all_icons()
        else:
            state.paused_by_schedule = False
            state.paused_by_battery = False
            if state.token is None:
                _load_active_token()
            if state.token:
                snapshot = fetch_best(state.token, plan=state.plan, model=config.MODEL)
                # Stale-token guard: Claude Code rotates OAuth access tokens
                # periodically. If our cached token was rotated out from under
                # us, the API returns 401/403. Re-read the credentials file
                # once and retry the poll — costs one wasted request per
                # rotation event, but keeps the tray live across rotations
                # without forcing the user to restart.
                if (snapshot.status_code in (401, 403)
                        and not snapshot.has_data):
                    _load_active_token()
                    if state.token:
                        snapshot = fetch_best(state.token, plan=state.plan, model=config.MODEL)
                state.snapshot = snapshot
                acct_id = state.active_account["id"] if state.active_account else "unknown"
                history.record(acct_id, snapshot)
                state.burn = history.burn_rate(60, acct_id)
                _check_notifications(icon, snapshot)
                _sample_active_window(snapshot)
            _refresh_all_icons()

        _maybe_prune()
        state.force_refresh.clear()
        state.force_refresh.wait(timeout=_effective_interval())


def _effective_interval() -> int:
    """
    Seconds to wait before the next poll.

    The usage endpoint rate-limits by User-Agent and starts returning 429s
    when polled faster than ~3 minutes. Every poll tries that endpoint first —
    even the ones that ended up on the header fallback — so the floor applies
    to all of them, not just to polls that succeeded there.
    """
    interval = int(user_settings.get("poll_interval_seconds",
                                     config.POLL_INTERVAL_SECONDS))
    return max(MIN_POLL_SECONDS, interval)


def _maybe_prune():
    now = time.time()
    if now - state.last_prune < 3600:
        return
    state.last_prune = now
    retention = int(user_settings.get("history_retention_days", 7))
    history.prune(retention)


def _refresh_icon(icon: pystray.Icon):
    metric = _metric_for(icon)
    try:
        icon.icon = render_icon(state.headline_pct_for(metric),
                                error=state.is_error,
                                theme=_current_theme(),
                                style=_current_icon_style_for(icon))
    except Exception:
        _log_action_error("_refresh_icon:icon")
    try:
        icon.title = _build_tooltip(metric)[:_TOOLTIP_MAX]
    except Exception:
        _log_action_error("_refresh_icon:title")
    try:
        icon.menu = build_menu(metric)
        icon.update_menu()
    except Exception:
        _log_action_error("_refresh_icon:menu")
    try:
        if tk_host.is_ready():
            tk_host.spawn(lambda _root: flyout.refresh_all())
    except Exception:
        _log_action_error("_refresh_icon:flyout")


def _refresh_all_icons():
    for ic in ICONS:
        _refresh_icon(ic)


def _current_icon_style_for(icon) -> str:
    """Per-icon style override (so dual-icon mode can render frame for 5h
    and donut for weekly), with fallback to the global setting."""
    override = getattr(icon, "style_override", None)
    if override:
        from icon_renderer import STYLES
        if override in STYLES:
            return override
    return _current_icon_style()


# Win32 tray tooltip (NOTIFYICONDATAW.szTip) is capped at 128 wide chars
# including the null terminator. Stay well under to leave headroom for
# the multi-line newline expansion Windows does internally.
_TOOLTIP_MAX = 120


def _truncate(text: str, limit: int = _TOOLTIP_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[int], width: int = 16) -> str:
    """Render a list of 0-100 values as a compact Unicode bar string.
    Empty list → empty string. Trims/right-aligns to `width` newest samples."""
    pts = [v for v in values if v is not None]
    if not pts:
        return ""
    pts = pts[-width:]
    return "".join(_SPARK_CHARS[min(7, max(0, v * 8 // 100))] for v in pts)


def _headline_sparkline(metric: str = "session") -> str:
    """Sparkline of the bucket this icon shows, over the last 24h."""
    if not bool(user_settings.get("show_sparkline", True)):
        return ""
    acct = state.active_account
    if not acct:
        return ""
    claim = state.headline_claim_for(_normalize_metric(metric))
    key = claim.key if claim else "five_hour"
    try:
        points = history.series(key, 24, acct["id"])
    except Exception:
        return ""
    return _sparkline([p[1] for p in points], width=16)


def _build_tooltip(metric: str = "session") -> str:
    if state.token_error:
        return _truncate(f"{config.APP_NAME}\n{t('status.token_error_tooltip')}")
    if state.paused_by_schedule:
        return _truncate(f"{config.APP_NAME}\n{t('status.paused_tooltip')}")
    if state.paused_by_battery:
        return _truncate(f"{config.APP_NAME}\n{t('status.battery_tooltip')}")
    snap = state.snapshot
    if snap is None:
        return _truncate(f"{config.APP_NAME}\n{t('status.fetching_tooltip')}")
    if not snap.ok:
        return _truncate(
            f"{config.APP_NAME}\n"
            f"{t('status.error_tooltip', msg=snap.error or 'unknown')}"
        )

    headline = state.headline_claim_for(_normalize_metric(metric))
    tag = f"[{_compact_label(headline)}]" if headline else "[5h]"
    # The tag is what tells the two icons apart, so it always stays. The
    # account name only earns its characters when there is more than one.
    if len(accounts.list_accounts()) > 1 and state.active_account:
        header = f"{tag} {state.active_account['name']}"
    else:
        header = tag
    if state.plan:
        header += f" · {state.plan}"

    # Headline bucket first, then the rest — the Win32 tooltip is short, so
    # per-model buckets get the compact label (just "Opus", "Fable", ...).
    ordered = snap.ordered_claims()
    if headline:
        ordered = [headline] + [c for c in ordered if c.key != headline.key]

    claim_lines = [
        f"{_compact_label(claim)} {claim.pct}% → {format_reset(claim.reset_seconds)}"
        for claim in ordered
    ]
    if not claim_lines:
        return _truncate(f"{header}\n{t('status.no_headers')}")

    optional = []
    spark = _headline_sparkline(metric)
    if spark:
        optional.append(f"24h: {spark}")
    if bool(user_settings.get("show_cost", True)):
        usd = cost.today_usd()
        if usd is not None:
            optional.append(f"Today: {cost.format_cost(usd)}")

    return _fit_lines(header, claim_lines, optional)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units, which is what szTip actually counts.

    An emoji in an account name is one Python character but two units, so
    counting characters could let the buffer overflow.
    """
    return len(text.encode("utf-16-le")) // 2


def _fit_lines(header: str, claim_lines: list[str],
               optional: list[str], limit: int = _TOOLTIP_MAX) -> str:
    """Fit whole lines into the tooltip budget — never half a line.

    szTip is a fixed WCHAR[128]; one unit over and pystray raises inside its
    own thread and the icon never appears at all. Buckets are added first, the
    sparkline and cost last, and anything that does not fit is dropped
    entirely rather than cut mid-word.
    """
    lines = [header]

    def fits(extra: str) -> bool:
        return _utf16_len("\n".join(lines + [extra])) <= limit

    for line in claim_lines:
        if fits(line):
            lines.append(line)
    for line in optional:
        if fits(line):
            lines.append(line)
    return "\n".join(lines)


def _eta_summary() -> Optional[str]:
    bits = []
    for key in _live_claim_keys():
        label = claim_label(key)
        info = state.burn.get(key, {})
        eta = info.get("eta_seconds")
        rate = info.get("rate")
        if eta is not None and rate is not None:
            bits.append(t('bar.burn_full_in', label=label,
                          rate=rate, eta=format_reset(eta)))
    return " · ".join(bits) if bits else None


def _sample_active_window(snap: UsageSnapshot):
    """Hook for the active-window-attribution feature. Filled in by attribution.py
    if `attribute_active_window` setting is enabled."""
    if not bool(user_settings.get("attribute_active_window", False)):
        return
    try:
        import attribution
        attribution.record(snap)
    except Exception:
        _log_action_error("attribution.record")


def _live_claim_keys() -> list[str]:
    """Buckets with data in the latest snapshot."""
    snap = state.snapshot
    if not snap:
        return []
    return [c.key for c in snap.ordered_claims()]


def _check_notifications(icon: pystray.Icon, snap: UsageSnapshot):
    if not snap.ok or not snap.has_data:
        return

    thresholds = _thresholds()
    play_sound = bool(user_settings.get("sound_alerts", True))
    # Every bucket alerts on its own — a full Opus week matters even while
    # the all-model week still has room.
    pairs = [(c.key, c.pct, _label(c)) for c in snap.ordered_claims()]
    for key, pct, label in pairs:
        if pct is None:
            continue
        fired = state.fired_thresholds[key]
        fired.intersection_update({th for th in thresholds if pct >= th})
        for threshold in thresholds:
            if pct >= threshold and threshold not in fired:
                fired.add(threshold)
                notifications.notify(
                    icon,
                    t('toast.heads_up_title', app=config.APP_NAME),
                    t('toast.heads_up_body', label=label, pct=pct),
                )
                if play_sound:
                    sound.play_alert()

    # ETA-based alert — fire once when projection crosses the warning
    # window, reset with hysteresis when projection eases off again.
    eta_min = int(user_settings.get("eta_alert_minutes", 60))
    if eta_min > 0:
        warn_s = eta_min * 60
        reset_s = warn_s * 2
        for key, _pct, label in pairs:
            info = state.burn.get(key, {}) or {}
            eta = info.get("eta_seconds")
            if eta is None:
                state.fired_eta[key] = False
                continue
            if eta > reset_s:
                state.fired_eta[key] = False
                continue
            if eta <= warn_s and not state.fired_eta[key]:
                state.fired_eta[key] = True
                notifications.notify(
                    icon,
                    t('toast.eta_warning_title', app=config.APP_NAME),
                    t('toast.eta_warning_body', label=label,
                      eta=format_reset(int(eta))),
                )
                if play_sound:
                    sound.play_alert()


# --- Menu actions ---------------------------------------------------------

def action_refresh(icon, item):
    state.force_refresh.set()


def action_copy_pct(icon, item):
    snap = state.snapshot
    if not snap or not snap.has_data:
        return
    parts = [f"{_compact_label(c)} {c.pct}%" for c in snap.ordered_claims()]
    text = " · ".join(parts)
    try:
        import pyperclip
        pyperclip.copy(text)
        notifications.notify(icon, config.APP_NAME, t('toast.copied', text=text))
    except Exception:
        _log_action_error("action_copy_pct")


def _log_action_error(where: str) -> None:
    try:
        log = user_settings.SETTINGS_DIR / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def _toggle_flyout(icon, root) -> None:
    """Open or close this icon's panel, on the Tk thread.

    The metric is read at render time rather than captured, so switching the
    icon's bucket from the menu reaches a panel that is already open.
    """
    panel = flyout.for_icon(icon, root,
                            lambda: _current_data(_metric_for(icon)))
    if panel is not None:
        panel.toggle()


def action_show_status(icon, item):
    """Left click: toggle the flyout. It renders its own error states, so a
    missing token no longer short-circuits to a notification."""
    try:
        if tk_host.is_ready():
            tk_host.spawn(lambda root: _toggle_flyout(icon, root))
            return
    except Exception:
        _log_action_error("action_show_status:flyout")

    # Fallback: open the proven history window instead.
    try:
        if state.active_account:
            history_window.show(
                state.active_account["id"],
                state.active_account["name"],
                get_data=_current_data,
            )
            return
    except Exception:
        _log_action_error("action_show_status:history_window")

    # Last resort: notification with current numbers.
    snap = state.snapshot
    parts = [f"{_label(c)}: {c.pct}%"
             for c in (snap.ordered_claims() if snap else [])]
    notifications.notify(icon, config.APP_NAME,
                         " · ".join(parts) or t('status.no_data'))


def action_show_error(icon, item):
    msg = state.token_error or (state.snapshot.error if state.snapshot else None)
    if msg:
        notifications.notify(icon,
                             t('toast.error_title', app=config.APP_NAME),
                             msg[:200])


def action_quit(icon, item):
    state.stop.set()
    state.force_refresh.set()
    for ic in ICONS:
        try:
            ic.stop()
        except Exception:
            pass
    try:
        if tk_host.is_ready():
            tk_host.spawn(lambda _root: flyout.destroy_all())
    except Exception:
        pass
    tk_host.stop()


def action_open_repo(icon, item):
    webbrowser.open(REPO_URL)


def action_open_console_usage(icon, item):
    webbrowser.open(CONSOLE_USAGE_URL)


def action_open_console_limits(icon, item):
    webbrowser.open(CONSOLE_LIMITS_URL)


def _current_data(metric: str = "session") -> dict:
    """Everything the flyout and the history window render.

    The default argument keeps `history_window.show(get_data=_current_data)`
    working, which calls it with no arguments.
    """
    snap = state.snapshot
    claims_out = [
        {
            "key": c.key,
            "label": _label(c),
            # The flyout puts one bucket per row under a shared window, so the
            # scope alone reads better there than "Weekly · Fable".
            "short_label": _compact_label(c),
            "pct": c.pct,
            "reset": c.reset_seconds,
            "cap_fraction": c.cap_fraction,
        }
        for c in (snap.ordered_claims() if snap else [])
    ]
    return {
        # Every bucket, for the popup that renders one bar each.
        "claims": claims_out,
        "source": snap.source if snap else None,
        "extra_usage": snap.extra_usage if snap else None,
        # Where the weekly window went, by surface (Claude Code / Chats / ...).
        "weekly_breakdown": (snap.meta or {}).get("weekly_breakdown") if snap else None,
        # Legacy keys kept for older window code.
        "session_pct": snap.session_pct if snap else None,
        "weekly_pct": snap.weekly_pct if snap else None,
        "session_reset": snap.session_reset_seconds if snap else None,
        "weekly_reset": snap.weekly_reset_seconds if snap else None,
        "burn": state.burn,
        "plan": state.plan,
        # Flyout state.
        "account": state.active_account["name"] if state.active_account else None,
        "headline_key": (lambda c: c.key if c else None)(
            state.headline_claim_for(_normalize_metric(metric))),
        "known_keys": _available_metrics(),
        "fetched_at": snap.fetched_at if snap else None,
        # None means "no reading yet", which is not the same as a failed one —
        # the flyout paints those two states differently.
        "ok": bool(snap.ok) if snap else None,
        "error": snap.error if snap else None,
        "token_error": state.token_error,
        "paused": ("schedule" if state.paused_by_schedule
                   else "battery" if state.paused_by_battery else None),
        "poll_interval": _effective_interval(),
        "thresholds": _thresholds(),
    }


def action_show_history(icon, item):
    if not state.active_account:
        notifications.notify(icon, config.APP_NAME, t('status.no_account'))
        return
    history_window.show(
        state.active_account["id"],
        state.active_account["name"],
        get_data=_current_data,
    )


def _on_settings_changed(icon: pystray.Icon):
    def _cb():
        state.fired_thresholds = defaultdict(set)
        _load_active_token()
        state.force_refresh.set()
        try:
            _refresh_icon(icon)
        except Exception:
            pass
    return _cb


def action_manage_accounts(icon, item):
    settings_dialogs.open_accounts(_on_settings_changed(icon))


def action_edit_schedule(icon, item):
    settings_dialogs.open_schedule(_on_settings_changed(icon))


def action_edit_thresholds(icon, item):
    settings_dialogs.open_thresholds(_on_settings_changed(icon))


def _make_switch_account(account_id: str):
    def _do(icon, item):
        accounts.set_active(account_id)
        state.fired_thresholds = defaultdict(set)
        _load_active_token()
        state.force_refresh.set()
        _refresh_icon(icon)
    return _do


def _make_set_threshold_preset(preset: list[int]):
    def _do(icon, item):
        user_settings.update(thresholds=preset)
        state.fired_thresholds = defaultdict(set)
        _refresh_icon(icon)
    return _do


def _make_set_theme(value: str):
    def _do(icon, item):
        user_settings.update(theme=value)
        _refresh_icon(icon)
    return _do


def _make_set_icon_style(value: str):
    def _do(icon, item):
        user_settings.update(icon_style=value)
        _refresh_icon(icon)
    return _do


def _make_set_metric(value: str):
    """Point this icon at a bucket ("auto" = whichever is closest to full)."""
    def _do(icon, item):
        user_settings.update(headline_metric=value)
        if getattr(icon, "metric_override", None) is not None:
            icon.metric_override = value
        state.force_refresh.set()
        _refresh_icon(icon)
    return _do


def _make_set_interval(seconds: int):
    def _do(icon, item):
        user_settings.update(poll_interval_seconds=seconds)
        state.force_refresh.set()
        _refresh_icon(icon)
    return _do


def _restart_app(icon) -> None:
    """Spawn a replacement instance, then shut this one down.

    Used after settings changes that require a full UI rebuild (e.g.,
    language) because pystray on Windows can't reliably swap an active
    Win32 popup menu from a non-message-loop thread.
    """
    try:
        if getattr(sys, "frozen", False):
            # Bundled exe — just relaunch self
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, **kwargs)
    except Exception:
        _log_action_error("restart_spawn")
        return

    # Brief delay so the replacement instance has time to come up before
    # we remove our tray icon — avoids a visible gap.
    time.sleep(0.6)
    state.stop.set()
    state.force_refresh.set()
    for ic in ICONS:
        try:
            ic.stop()
        except Exception:
            pass
    tk_host.stop()


def _make_set_language(code: str):
    def _do(icon, item):
        if code == user_settings.get("language"):
            return
        set_language(code)
        notifications.notify(
            icon,
            config.APP_NAME,
            f"{t('common.save')}: {LANGUAGES.get(code, code)}",
        )
        # Restart on a worker thread so we don't block this menu callback
        # while we sleep and then tear down pystray.
        threading.Thread(
            target=_restart_app, args=(icon,), daemon=True,
        ).start()
    return _do


def action_toggle_sound(icon, item):
    cur = bool(user_settings.get("sound_alerts", True))
    user_settings.update(sound_alerts=not cur)
    _refresh_icon(icon)


def action_toggle_schedule(icon, item):
    sched = dict(user_settings.get("schedule", {}) or {})
    sched["enabled"] = not bool(sched.get("enabled"))
    user_settings.update(schedule=sched)
    state.force_refresh.set()
    _refresh_icon(icon)


def _make_bool_toggle(key: str, default: bool = True):
    def _do(icon, item):
        try:
            cur = bool(user_settings.get(key, default))
            user_settings.update(**{key: not cur})
            state.force_refresh.set()
            # Defer menu rebuild slightly — touching icon.menu from inside a
            # Win32 menu-callback while the menu is still on screen has been
            # observed to kill the message loop on some Win11 builds.
            # force_refresh.set() above already triggers the poll thread to
            # re-render once the menu closes; don't double up here.
        except Exception:
            _log_action_error(f"toggle:{key}")
    return _do


action_toggle_battery_pause = _make_bool_toggle("pause_on_battery", True)
action_toggle_sparkline = _make_bool_toggle("show_sparkline", True)
action_toggle_cost = _make_bool_toggle("show_cost", True)
action_toggle_window_attribution = _make_bool_toggle("attribute_active_window", False)


# --- Menu construction ----------------------------------------------------

def build_menu(metric: str = "session"):
    metric = _normalize_metric(metric)
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: _menu_headline_text(metric),
            None,
            enabled=False,
        ),
        *[
            pystray.MenuItem(
                lambda item, i=slot: _menu_claim_text(i),
                None,
                enabled=False,
                visible=lambda item, i=slot: bool(_menu_claim_text(i)),
            )
            for slot in range(_MAX_CLAIM_ROWS)
        ],
        pystray.MenuItem(
            lambda item: _menu_burn_text(),
            None,
            enabled=False,
            visible=lambda item: bool(_menu_burn_text()),
        ),
        pystray.MenuItem(
            lambda item: _menu_cost_text(),
            None,
            enabled=False,
            visible=lambda item: bool(_menu_cost_text()),
        ),
        pystray.MenuItem(
            lambda item: _menu_attribution_text(),
            None,
            enabled=False,
            visible=lambda item: bool(_menu_attribution_text()),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(t('menu.show_status'), action_show_status, default=True),
        pystray.MenuItem(t('menu.show_history'), action_show_history),
        pystray.MenuItem(t('menu.refresh_now'), action_refresh),
        pystray.MenuItem(t('menu.copy_pct'), action_copy_pct,
                         visible=lambda item: state.snapshot is not None
                                 and state.snapshot.has_data),
        pystray.MenuItem(
            t('menu.show_last_error'),
            action_show_error,
            visible=lambda item: state.is_error,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(t('menu.account'), _build_account_menu()),
        pystray.MenuItem(t('menu.settings'), _build_settings_menu(metric)),
        pystray.MenuItem(t('menu.open_console'), _build_console_menu()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Powered by KPWebappStudio", action_open_repo),
        pystray.MenuItem(t('menu.quit', app=config.APP_NAME), action_quit),
    )


def _build_account_menu():
    items = []
    active = state.active_account
    active_id = active["id"] if active else None
    for acct in accounts.list_accounts():
        items.append(pystray.MenuItem(
            acct.get("name", "Account"),
            _make_switch_account(acct["id"]),
            checked=lambda item, aid=acct["id"]: aid == active_id,
            radio=True,
        ))
    if items:
        items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem(t('menu.manage_accounts'), action_manage_accounts))
    return pystray.Menu(*items)


def _build_bucket_menu(metric: str):
    """Radio list of the buckets this icon can display."""
    items = [
        pystray.MenuItem(
            t('menu.bucket_auto'),
            _make_set_metric("auto"),
            checked=lambda item: metric == "auto",
            radio=True,
        ),
        pystray.Menu.SEPARATOR,
    ]
    for key in _available_metrics():
        items.append(pystray.MenuItem(
            claim_label(key),
            _make_set_metric(key),
            checked=lambda item, k=key: metric == k,
            radio=True,
        ))
    return pystray.Menu(*items)


def _build_settings_menu(metric: str = "session"):
    cur_thresholds = _thresholds()
    presets = [
        (t('menu.thresholds_quiet'), [95]),
        (t('menu.thresholds_default'), [80, 95]),
        (t('menu.thresholds_sensitive'), [50, 75, 90]),
    ]
    threshold_items = [
        pystray.MenuItem(
            label,
            _make_set_threshold_preset(values),
            checked=lambda item, v=values: list(v) == cur_thresholds,
            radio=True,
        )
        for label, values in presets
    ]

    cur_theme = user_settings.get("theme", "auto")
    theme_items = [
        pystray.MenuItem(
            label,
            _make_set_theme(value),
            checked=lambda item, v=value: v == cur_theme,
            radio=True,
        )
        for label, value in (
            (t('menu.theme_auto'), "auto"),
            (t('menu.theme_light'), "light"),
            (t('menu.theme_dark'), "dark"),
        )
    ]

    cur_style = _current_icon_style()
    style_items = [
        pystray.MenuItem(
            label,
            _make_set_icon_style(value),
            checked=lambda item, v=value: v == cur_style,
            radio=True,
        )
        for label, value in (
            (t('menu.style_frame'), "frame"),
            (t('menu.style_solid'), "solid"),
            (t('menu.style_donut'), "donut"),
            (t('menu.style_bar'), "bar"),
        )
    ]

    cur_interval = int(user_settings.get("poll_interval_seconds", 60))
    interval_items = [
        pystray.MenuItem(
            label,
            _make_set_interval(seconds),
            checked=lambda item, s=seconds: s == cur_interval,
            radio=True,
        )
        # Floored at MIN_POLL_SECONDS while the usage endpoint is the source,
        # so shorter presets would be silently clamped.
        for label, seconds in (
            (t('menu.interval_3m'), 180),
            (t('menu.interval_5m'), 300),
            (t('menu.interval_10m'), 600),
            (t('menu.interval_30m'), 1800),
        )
    ]

    from i18n import current_language
    cur_lang = current_language()
    language_items = [
        pystray.MenuItem(
            label,
            _make_set_language(code),
            checked=lambda item, c=code: c == cur_lang,
            radio=True,
        )
        for code, label in LANGUAGES.items()
    ]

    sched = user_settings.get("schedule", {}) or {}
    sched_label = t(
        'menu.pause_outside',
        start=int(sched.get('start_hour', 9)),
        end=int(sched.get('end_hour', 18)),
    )

    threshold_items.append(pystray.Menu.SEPARATOR)
    threshold_items.append(pystray.MenuItem(t('menu.thresholds_custom'),
                                            action_edit_thresholds))

    return pystray.Menu(
        pystray.MenuItem(t('menu.alert_thresholds'),
                         pystray.Menu(*threshold_items)),
        pystray.MenuItem(
            t('menu.sound_alerts'),
            action_toggle_sound,
            checked=lambda item: bool(user_settings.get("sound_alerts", True)),
        ),
        pystray.MenuItem(
            sched_label,
            action_toggle_schedule,
            checked=lambda item: bool(
                (user_settings.get("schedule", {}) or {}).get("enabled")
            ),
        ),
        pystray.MenuItem(t('menu.schedule_settings'), action_edit_schedule),
        pystray.MenuItem(
            t('menu.pause_on_battery'),
            action_toggle_battery_pause,
            checked=lambda item: bool(user_settings.get("pause_on_battery", True)),
        ),
        pystray.MenuItem(
            t('menu.show_sparkline'),
            action_toggle_sparkline,
            checked=lambda item: bool(user_settings.get("show_sparkline", True)),
        ),
        pystray.MenuItem(
            t('menu.show_cost'),
            action_toggle_cost,
            checked=lambda item: bool(user_settings.get("show_cost", True)),
        ),
        pystray.MenuItem(
            t('menu.attribute_window'),
            action_toggle_window_attribution,
            checked=lambda item: bool(user_settings.get("attribute_active_window", False)),
        ),
        pystray.MenuItem(t('menu.icon_bucket'), _build_bucket_menu(metric)),
        pystray.MenuItem(t('menu.icon_theme'), pystray.Menu(*theme_items)),
        pystray.MenuItem(t('menu.icon_style'), pystray.Menu(*style_items)),
        pystray.MenuItem(t('menu.poll_interval'),
                         pystray.Menu(*interval_items)),
        pystray.MenuItem(t('menu.language'), pystray.Menu(*language_items)),
    )


def _build_console_menu():
    return pystray.Menu(
        pystray.MenuItem(t('menu.console_usage'), action_open_console_usage),
        pystray.MenuItem(t('menu.console_limits'), action_open_console_limits),
    )


# Bar rows the menu reserves for buckets. Plans expose at most a handful
# (5h, weekly, and one per model family); extra ones fall off the list.
_MAX_CLAIM_ROWS = 6


def _menu_headline_text(metric: str = "session") -> str:
    snap = state.snapshot
    if state.token_error:
        return t('status.token_error')
    if state.paused_by_schedule:
        return t('status.paused')
    if state.paused_by_battery:
        return t('status.battery')
    if snap is None:
        return t('status.fetching')
    if not snap.ok:
        return t('status.api_error')
    name = state.active_account["name"] if state.active_account else config.APP_NAME
    headline = state.headline_claim_for(_normalize_metric(metric))
    tag = f"[{_compact_label(headline)}]" if headline else "[5h]"
    parts = [f"● {tag} {name}"]
    if state.plan:
        parts.append(state.plan)
    if snap.source == "headers":
        parts.append(t('status.header_fallback'))
    return " · ".join(parts)


def _menu_claim_text(slot: int) -> str:
    """One bar row per bucket. Slots beyond the current bucket count stay blank."""
    snap = state.snapshot
    if not snap or not snap.ok:
        return ""
    rows = snap.ordered_claims()
    if slot >= len(rows):
        return ""
    claim = rows[slot]
    pct = claim.pct
    text = (
        f"{color_emoji(pct)} {_label(claim)}  {unicode_bar(pct)}  {pct:>3}%  "
        f"· {t('bar.resets_in', time=format_reset(claim.reset_seconds))}"
    )
    if claim.cap_fraction:
        text += f" · {t('bar.cap_note', pct=int(claim.cap_fraction * 100))}"
    return text


def _menu_burn_text() -> str:
    bits = []
    for key in _live_claim_keys():
        label = claim_label(key)
        info = state.burn.get(key, {})
        rate = info.get("rate")
        eta = info.get("eta_seconds")
        # Flat buckets say nothing useful and there are several of them now.
        if rate is None or rate <= 0.05:
            continue
        if eta is not None:
            bits.append(f"{label}: +{rate:.0f}%/h → {format_reset(eta)}")
        else:
            bits.append(f"{label}: +{rate:.0f}%/h")
    return " · ".join(bits)


def _menu_cost_text() -> str:
    if not bool(user_settings.get("show_cost", True)):
        return ""
    usd = cost.today_usd()
    if usd is None:
        return ""
    return f"💰 Today: {cost.format_cost(usd)}"


def _menu_attribution_text() -> str:
    if not bool(user_settings.get("attribute_active_window", False)):
        return ""
    try:
        import attribution
        rows = attribution.top_recent(hours=1.0, limit=3)
    except Exception:
        return ""
    if not rows:
        return ""
    bits = [f"{title} +{delta}%" for title, delta in rows]
    return "🪟 1h: " + " · ".join(bits)


# --- Entry point ----------------------------------------------------------

def _redirect_stderr_to_log() -> None:
    """When running under pythonw.exe there is no console; redirect stderr to
    a file so we can see unhandled tracebacks instead of the process
    silently disappearing."""
    try:
        log_path = user_settings.SETTINGS_DIR / "error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stderr = stream
        sys.stderr.write(
            f"\n=== started {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"pid={__import__('os').getpid()} ===\n"
        )
    except Exception:
        pass


def _start_heartbeat_thread() -> None:
    """Touch a heartbeat file every 30s so an external watchdog can tell
    whether this tray instance is still alive.

    A PID file would be simpler, but the Microsoft-Store Python install
    runs the interpreter inside an App Container, so ``os.getpid()``
    returns a container-internal PID that the host's process table can
    never see. Heartbeat-file mtime sidesteps the PID-namespace mismatch
    entirely.
    """
    # If the poll loop hasn't ticked in this long, treat it as wedged and
    # stop refreshing the heartbeat so the external watchdog restarts us.
    # Must stay above the configured poll interval, which the bucket work
    # raised as high as 30 minutes — a slow interval is not a wedge.
    def _wedge_threshold() -> int:
        interval = int(user_settings.get("poll_interval_seconds",
                                         config.POLL_INTERVAL_SECONDS))
        return max(600, interval * 2 + 120)

    def _beat():
        hb = user_settings.SETTINGS_DIR / "tray.heartbeat"
        try:
            hb.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        while not state.stop.is_set():
            tick = state.last_loop_tick
            poll_wedged = tick and (time.monotonic() - tick) > _wedge_threshold()
            if not poll_wedged:
                try:
                    hb.touch(exist_ok=True)
                    _os = __import__("os")
                    _os.utime(hb, None)
                except Exception:
                    pass
            state.stop.wait(timeout=30)

    threading.Thread(target=_beat, daemon=True).start()


def _make_icon(app_id: str, metric: str, style: Optional[str]) -> pystray.Icon:
    icon = pystray.Icon(
        app_id,
        icon=render_icon(None, theme=_current_theme(),
                         style=style or _current_icon_style()),
        title=f"{config.APP_NAME}\nStarting…",
        menu=build_menu(metric),
    )
    icon.metric_override = _normalize_metric(metric)
    if style:
        icon.style_override = style
    return icon


_instance_lock_handle = None


def _acquire_single_instance_lock() -> bool:
    """Per-data-dir exclusive lock: a 2nd copy of THIS instance (same
    CQT_DATA_DIR) refuses to start, while main + weekly (different data dirs)
    still coexist. The OS frees the lock when the process exits, so a crash
    never leaves it stale. No-op off Windows."""
    global _instance_lock_handle
    if sys.platform != "win32":
        return True
    import msvcrt
    try:
        user_settings.SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(user_settings.SETTINGS_DIR / "instance.lock", "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _instance_lock_handle = fh  # keep handle alive for process lifetime
        return True
    except OSError:
        return False


def main():
    import os as _os
    _redirect_stderr_to_log()

    if not _acquire_single_instance_lock():
        try:
            sys.stderr.write(
                f"=== duplicate instance for {user_settings.SETTINGS_DIR} — "
                f"exiting {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
        except Exception:
            pass
        return

    try:
        user_settings.load()
        state.last_loop_tick = time.monotonic()
        _start_heartbeat_thread()
        notifications.init(config.APP_NAME)

        # Tk owns the main thread. Create the hidden root BEFORE pystray
        # starts so any early popup request from the poller has somewhere
        # to land. See tk_host.py for why this matters (Tcl interpreter
        # thread-affinity invariant; creating tk.Tk() on a worker thread
        # eventually triggers a hard Tcl_Panic inside tcl86t.dll).
        tk_host.ensure()

        dual = _os.environ.get("CQT_DUAL_ICON", "").lower() in ("1", "true", "yes")

        if dual:
            # Single process, two icons — 5h (frame) + weekly (donut).
            # Both pystray Icons run on their own daemon threads; Tk owns
            # the main thread.
            ic_session = _make_icon(config.APP_ID, "session", "frame")
            ic_weekly = _make_icon(config.APP_ID + "Weekly", "weekly", "donut")
            ICONS.append(ic_session)
            ICONS.append(ic_weekly)
            threading.Thread(target=ic_session.run, daemon=True).start()
            threading.Thread(target=ic_weekly.run, daemon=True).start()
            threading.Thread(
                target=poll_loop, args=(ic_session,), daemon=True,
            ).start()
        else:
            metric = user_settings.get("headline_metric", "session")
            ic = _make_icon(config.APP_ID, metric, None)
            ICONS.append(ic)
            threading.Thread(target=ic.run, daemon=True).start()
            threading.Thread(
                target=poll_loop, args=(ic,), daemon=True,
            ).start()

        # Block main thread on Tk mainloop until action_quit / _restart_app
        # schedules root.quit() via tk_host.stop().
        tk_host.run()

        try:
            sys.stderr.write(
                f"=== tk_host.run() returned cleanly "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
        except Exception:
            pass
    except Exception:
        try:
            sys.stderr.write(
                f"\n!!! FATAL {time.strftime('%Y-%m-%d %H:%M:%S')} !!!\n"
            )
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
