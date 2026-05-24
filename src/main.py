"""
Claude Quota Tray — entry point.

A small system-tray app for Windows (and macOS/Linux) that polls Claude's
usage headers and displays the higher of session/weekly utilisation as a
coloured badge in the tray. Hover the icon for full details, right-click
for actions.
"""

import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import pystray

import config
import cost
import notifications
import settings as user_settings
import history
import sound
import theme as theme_mod
import accounts
import history_window
import status_window
import settings_dialogs
import tk_host
from i18n import LANGUAGES, set_language, t
from bar_widget import color_emoji, unicode_bar
from api_client import fetch_usage, format_reset, UsageSnapshot
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
        self.burn: dict = {"session": {}, "weekly": {}}
        self.force_refresh = threading.Event()
        self.stop = threading.Event()
        self.fired_thresholds = {"session": set(), "weekly": set()}
        self.last_prune = 0.0
        self.active_account: Optional[dict] = None
        self.plan: Optional[str] = None
        self.paused_by_schedule = False
        self.paused_by_battery = False
        self.fired_eta = {"session": False, "weekly": False}

    @property
    def headline_pct(self) -> Optional[int]:
        return self.headline_pct_for(user_settings.get("headline_metric", "session"))

    def headline_pct_for(self, metric: str) -> Optional[int]:
        """Number shown on the tray icon for a specific metric.

        `metric` is "session" (5h) or "weekly" (7d). Falls back to the other
        figure when the preferred one is missing.
        """
        if not self.snapshot or not self.snapshot.has_data:
            return None
        if metric == "weekly":
            if self.snapshot.weekly_pct is not None:
                return self.snapshot.weekly_pct
            return self.snapshot.session_pct
        if self.snapshot.session_pct is not None:
            return self.snapshot.session_pct
        return self.snapshot.weekly_pct

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
    """Read the per-icon metric override that was stamped at construction,
    falling back to the user_settings default for backward compatibility."""
    override = getattr(icon, "metric_override", None)
    if override in ("session", "weekly"):
        return override
    return user_settings.get("headline_metric", "session")


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
                snapshot = fetch_usage(state.token, model=config.MODEL)
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
                        snapshot = fetch_usage(state.token, model=config.MODEL)
                state.snapshot = snapshot
                acct_id = state.active_account["id"] if state.active_account else "unknown"
                history.record(acct_id, snapshot)
                state.burn = history.burn_rate(60, acct_id)
                _check_notifications(icon, snapshot)
                _sample_active_window(snapshot)
            _refresh_all_icons()

        _maybe_prune()
        interval = int(user_settings.get("poll_interval_seconds", config.POLL_INTERVAL_SECONDS))
        state.force_refresh.clear()
        state.force_refresh.wait(timeout=max(15, interval))


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
    """Sparkline of the given metric over the last 24h."""
    if not bool(user_settings.get("show_sparkline", True)):
        return ""
    acct = state.active_account
    if not acct:
        return ""
    idx = 2 if metric == "weekly" else 1
    try:
        rows = history.recent(24, acct["id"])
    except Exception:
        return ""
    return _sparkline([r[idx] for r in rows], width=16)


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

    name = state.active_account["name"] if state.active_account else config.APP_NAME
    tag = "[Week]" if metric == "weekly" else "[5h]"
    header = f"{tag} {name}"
    if state.plan:
        header += f" · {state.plan}"

    parts = []
    if snap.session_pct is not None:
        parts.append(
            f"{t('bar.session_short')} {snap.session_pct}% "
            f"→ {format_reset(snap.session_reset_seconds)}"
        )
    if snap.weekly_pct is not None:
        parts.append(
            f"{t('bar.weekly_short')} {snap.weekly_pct}% "
            f"→ {format_reset(snap.weekly_reset_seconds)}"
        )

    spark = _headline_sparkline(metric)
    if spark:
        parts.append(f"24h: {spark}")
    if bool(user_settings.get("show_cost", True)):
        usd = cost.today_usd()
        if usd is not None:
            parts.append(f"Today: {cost.format_cost(usd)}")
    body = "\n".join(parts) if parts else t('status.no_headers')
    return _truncate(f"{header}\n{body}")


def _eta_summary() -> Optional[str]:
    bits = []
    for key, label in (("session", t('bar.session_short')),
                       ("weekly", t('bar.weekly_short'))):
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


def _check_notifications(icon: pystray.Icon, snap: UsageSnapshot):
    if not snap.ok or not snap.has_data:
        return

    thresholds = _thresholds()
    play_sound = bool(user_settings.get("sound_alerts", True))
    pairs = [
        ("session", snap.session_pct, t('bar.session_short')),
        ("weekly", snap.weekly_pct, t('bar.weekly_short')),
    ]
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
    parts = []
    if snap.session_pct is not None:
        parts.append(f"5h {snap.session_pct}%")
    if snap.weekly_pct is not None:
        parts.append(f"Weekly {snap.weekly_pct}%")
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


def action_show_status(icon, item):
    """Left-click action: open the compact status popup with progress bars.
    Falls back to the full history window if the popup fails to spawn."""
    if state.token_error:
        notifications.notify(icon,
                             t('toast.token_error_title', app=config.APP_NAME),
                             state.token_error[:200])
        return
    name = state.active_account["name"] if state.active_account else config.APP_NAME
    try:
        ok = status_window.show(name, get_data=_current_data)
    except Exception:
        _log_action_error("action_show_status:status_window")
        ok = False

    if ok:
        return

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
    parts = []
    if snap and snap.session_pct is not None:
        parts.append(f"{t('bar.session_short')}: {snap.session_pct}%")
    if snap and snap.weekly_pct is not None:
        parts.append(f"{t('bar.weekly_short')}: {snap.weekly_pct}%")
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
    tk_host.stop()


def action_open_repo(icon, item):
    webbrowser.open(REPO_URL)


def action_open_console_usage(icon, item):
    webbrowser.open(CONSOLE_USAGE_URL)


def action_open_console_limits(icon, item):
    webbrowser.open(CONSOLE_LIMITS_URL)


def _current_data() -> dict:
    snap = state.snapshot
    return {
        "session_pct": snap.session_pct if snap else None,
        "weekly_pct": snap.weekly_pct if snap else None,
        "session_reset": snap.session_reset_seconds if snap else None,
        "weekly_reset": snap.weekly_reset_seconds if snap else None,
        "burn": state.burn,
        "plan": state.plan,
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
        state.fired_thresholds = {"session": set(), "weekly": set()}
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
        state.fired_thresholds = {"session": set(), "weekly": set()}
        _load_active_token()
        state.force_refresh.set()
        _refresh_icon(icon)
    return _do


def _make_set_threshold_preset(preset: list[int]):
    def _do(icon, item):
        user_settings.update(thresholds=preset)
        state.fired_thresholds = {"session": set(), "weekly": set()}
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
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: _menu_headline_text(metric),
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda item: _menu_session_text(),
            None,
            enabled=False,
            visible=lambda item: bool(_menu_session_text()),
        ),
        pystray.MenuItem(
            lambda item: _menu_weekly_text(),
            None,
            enabled=False,
            visible=lambda item: bool(_menu_weekly_text()),
        ),
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
        pystray.MenuItem(t('menu.settings'), _build_settings_menu()),
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


def _build_settings_menu():
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
        for label, seconds in (
            (t('menu.interval_30s'), 30),
            (t('menu.interval_1m'), 60),
            (t('menu.interval_2m'), 120),
            (t('menu.interval_5m'), 300),
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
    tag = "[Week]" if metric == "weekly" else "[5h]"
    if state.plan:
        return f"● {tag} {name} · {state.plan}"
    return f"● {tag} {name}"


def _menu_session_text() -> str:
    snap = state.snapshot
    if not snap or not snap.ok or snap.session_pct is None:
        return ""
    pct = snap.session_pct
    return (
        f"{color_emoji(pct)} {t('bar.session_short')}  {unicode_bar(pct)}  {pct:>3}%  "
        f"· {t('bar.resets_in', time=format_reset(snap.session_reset_seconds))}"
    )


def _menu_weekly_text() -> str:
    snap = state.snapshot
    if not snap or not snap.ok or snap.weekly_pct is None:
        return ""
    pct = snap.weekly_pct
    return (
        f"{color_emoji(pct)} {t('bar.weekly_short')}  {unicode_bar(pct)}  {pct:>3}%  "
        f"· {t('bar.resets_in', time=format_reset(snap.weekly_reset_seconds))}"
    )


def _menu_burn_text() -> str:
    bits = []
    for key, label in (("session", t('bar.session_short')),
                       (
                       "weekly", t('bar.weekly_short'))):
        info = state.burn.get(key, {})
        rate = info.get("rate")
        eta = info.get("eta_seconds")
        if rate is None:
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
    def _beat():
        hb = user_settings.SETTINGS_DIR / "tray.heartbeat"
        try:
            hb.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        while not state.stop.is_set():
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
    icon.metric_override = metric
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
