"""
Lightweight translation layer. Strings are looked up by key in nested
dictionaries. The active language is persisted via settings.py.

Adding a language: copy the ``en`` block, translate the values, and
register it in ``LANGUAGES`` below.
"""

from __future__ import annotations

import locale
from typing import Any

import settings as user_settings


LANGUAGES = {
    "en": "English",
    "th": "ไทย",
}


_T: dict[str, dict[str, str]] = {
    "en": {
        # Common
        "common.save": "Save",
        "common.cancel": "Cancel",
        "common.close": "Close",
        "common.refresh": "Refresh",
        "common.ok": "OK",
        "common.browse": "Browse…",
        "common.add": "Add…",
        "common.rename": "Rename",
        "common.remove": "Remove",

        # Status / tooltip
        "status.token_error": "⚠ Token not found",
        "status.paused": "⏸ Paused (schedule)",
        "status.battery": "🔋 Paused (on battery)",
        "status.fetching": "⏳ Fetching…",
        "status.api_error": "⚠ API error",
        "status.no_data": "No data",
        "status.token_error_tooltip": "Token error — right-click for details",
        "status.paused_tooltip": "Paused (outside scheduled hours)",
        "status.battery_tooltip": "Paused (laptop on battery)",
        "status.fetching_tooltip": "Fetching…",
        "status.error_tooltip": "Error: {msg}",
        "status.no_headers": "No rate-limit headers in response.",
        "status.no_account": "No account configured.",
        "status.fetching_msg": "Fetching usage data…",
        "status.unknown_error": "Unknown error",

        # Bar / labels
        "bar.session_label": "5-hour limit",
        "bar.weekly_label": "Weekly limit",
        "bar.session_short": "5-hour",
        "bar.weekly_short": "Weekly",
        "bar.no_data": "No data yet",
        "bar.resets_in": "resets in {time}",
        "bar.resets_unknown": "resets — unknown",
        "bar.burn_collecting": "Burn rate: collecting…",
        "bar.burn_full_in": "{label}: +{rate:.1f}%/h · full in {eta}",
        "bar.burn_no_eta": "{label}: +{rate:.1f}%/h",

        # Notifications
        "toast.token_error_title": "{app} — Token error",
        "toast.api_error_title": "{app} — API error",
        "toast.heads_up_title": "{app}: heads up",
        "toast.heads_up_body": "{label} limit at {pct}%",
        "toast.error_title": "{app} error",
        "toast.copied": "Copied: {text}",
        "toast.eta_warning_title": "{app}: trending toward cap",
        "toast.eta_warning_body": "{label} will reach 100% in ~{eta} at current rate",
        "toast.battery_paused": "On battery — polling paused",

        # Menu
        "menu.show_status": "Show status",
        "menu.show_history": "Show history…",
        "menu.refresh_now": "Refresh now",
        "menu.copy_pct": "Copy current %",
        "menu.pause_on_battery": "Pause polling on battery",
        "menu.show_sparkline": "Show sparkline in tooltip",
        "menu.show_cost": "Show today's cost (ccusage)",
        "menu.attribute_window": "Attribute usage to active window",
        "menu.show_last_error": "Show last error",
        "menu.account": "Account",
        "menu.manage_accounts": "Manage accounts…",
        "menu.settings": "Settings",
        "menu.alert_thresholds": "Alert thresholds",
        "menu.thresholds_quiet": "Quiet (95%)",
        "menu.thresholds_default": "Default (80, 95)",
        "menu.thresholds_sensitive": "Sensitive (50, 75, 90)",
        "menu.thresholds_custom": "Custom…",
        "menu.sound_alerts": "Sound alerts",
        "menu.pause_outside": "Pause outside {start}:00–{end}:00",
        "menu.schedule_settings": "Schedule settings…",
        "menu.icon_theme": "Icon theme",
        "menu.theme_auto": "Auto",
        "menu.theme_light": "Light",
        "menu.theme_dark": "Dark",
        "menu.icon_style": "Icon style",
        "menu.style_frame": "Frame",
        "menu.style_solid": "Solid",
        "menu.style_donut": "Donut",
        "menu.style_bar": "Bar",
        "menu.poll_interval": "Poll interval",
        "menu.interval_30s": "30 seconds",
        "menu.interval_1m": "1 minute",
        "menu.interval_2m": "2 minutes",
        "menu.interval_5m": "5 minutes",
        "menu.language": "Language",
        "menu.open_console": "Open Anthropic Console",
        "menu.console_usage": "Usage dashboard",
        "menu.console_limits": "Rate limits",
        "menu.quit": "Quit {app}",

        # Window titles
        "window.history_title": "Claude Quota — {name}",
        "window.status_title": "Claude Quota — {name}",
        "window.current_usage": "Current usage",
        "window.last_24h": "Last 24 hours",
        "window.no_history": "No history yet — keep the tray running for a few minutes.",

        # Plan badge
        "plan.label": "Plan",
        "plan.unknown": "Unknown",

        # Accounts dialog
        "dialog.accounts_title": "Claude Quota — Accounts",
        "dialog.accounts_heading": "Configured accounts",
        "dialog.accounts_desc": "Each account points to a Claude Code credentials file. “Default” auto-discovers ~/.claude/.credentials.json.",
        "dialog.accounts_no_selection": "Select an account first.",
        "dialog.accounts_keep_one": "At least one account must remain.",
        "dialog.accounts_remove_confirm": "Remove “{name}”?",
        "dialog.accounts_rename": "Rename",
        "dialog.accounts_rename_title": "Rename account",
        "dialog.accounts_rename_label": "New name:",
        "dialog.accounts_remove_title": "Remove account",
        "dialog.accounts_add_title": "Add account",
        "dialog.accounts_field_name": "Account name",
        "dialog.accounts_field_mode": "Mode",
        "dialog.accounts_mode_auto": "Auto-discover",
        "dialog.accounts_mode_file": "Credentials file…",
        "dialog.accounts_field_path": "Credentials file",
        "dialog.accounts_browse_title": "Select credentials.json",
        "dialog.accounts_missing_path": "Pick a credentials file or switch to Auto-discover.",

        # Schedule dialog
        "dialog.schedule_title": "Claude Quota — Schedule",
        "dialog.schedule_heading": "Polling schedule",
        "dialog.schedule_desc": "When enabled, the tray pauses API polling outside the hours and days you select below.",
        "dialog.schedule_enable": "Enable scheduled polling",
        "dialog.schedule_hours": "Active hours:",
        "dialog.schedule_days": "Active days",

        # Days (short)
        "day.mon": "Mon",
        "day.tue": "Tue",
        "day.wed": "Wed",
        "day.thu": "Thu",
        "day.fri": "Fri",
        "day.sat": "Sat",
        "day.sun": "Sun",

        # Thresholds dialog
        "dialog.thresholds_title": "Claude Quota — Alert thresholds",
        "dialog.thresholds_heading": "Alert thresholds",
        "dialog.thresholds_desc": "Notification fires when usage crosses any of these percentages.\nEnter values separated by commas (e.g., 50, 75, 90).",
        "dialog.thresholds_play_sound": "Play sound on alert",
        "dialog.thresholds_invalid": "Enter integer percentages between 1 and 100.",
        "dialog.thresholds_empty": "Enter at least one threshold.",
    },
    "th": {
        # Common
        "common.save": "บันทึก",
        "common.cancel": "ยกเลิก",
        "common.close": "ปิด",
        "common.refresh": "รีเฟรช",
        "common.ok": "ตกลง",
        "common.browse": "เลือกไฟล์…",
        "common.add": "เพิ่ม…",
        "common.rename": "เปลี่ยนชื่อ",
        "common.remove": "ลบ",

        # Status / tooltip
        "status.token_error": "⚠ ไม่พบ Token",
        "status.paused": "⏸ หยุดชั่วคราว (ตามตาราง)",
        "status.battery": "🔋 หยุดชั่วคราว (ใช้แบตเตอรี่)",
        "status.fetching": "⏳ กำลังโหลด…",
        "status.api_error": "⚠ API ผิดพลาด",
        "status.no_data": "ไม่มีข้อมูล",
        "status.token_error_tooltip": "ไม่พบ Token — คลิกขวาเพื่อดูรายละเอียด",
        "status.paused_tooltip": "หยุดชั่วคราว (อยู่นอกเวลาที่ตั้งไว้)",
        "status.battery_tooltip": "หยุดชั่วคราว (laptop ใช้แบตเตอรี่)",
        "status.fetching_tooltip": "กำลังโหลด…",
        "status.error_tooltip": "ผิดพลาด: {msg}",
        "status.no_headers": "ไม่พบ rate-limit headers จาก response",
        "status.no_account": "ยังไม่ได้ตั้งค่าบัญชี",
        "status.fetching_msg": "กำลังโหลดข้อมูลโควต้า…",
        "status.unknown_error": "ผิดพลาดไม่ทราบสาเหตุ",

        # Bar / labels
        "bar.session_label": "ลิมิตรอบ 5 ชั่วโมง",
        "bar.weekly_label": "ลิมิตรายสัปดาห์",
        "bar.session_short": "5 ชม.",
        "bar.weekly_short": "รายสัปดาห์",
        "bar.no_data": "ยังไม่มีข้อมูล",
        "bar.resets_in": "รีเซ็ตในอีก {time}",
        "bar.resets_unknown": "รีเซ็ต — ไม่ทราบเวลา",
        "bar.burn_collecting": "อัตราการใช้: กำลังเก็บข้อมูล…",
        "bar.burn_full_in": "{label}: +{rate:.1f}%/ชม. · เต็มในอีก {eta}",
        "bar.burn_no_eta": "{label}: +{rate:.1f}%/ชม.",

        # Notifications
        "toast.token_error_title": "{app} — ไม่พบ Token",
        "toast.api_error_title": "{app} — API ผิดพลาด",
        "toast.heads_up_title": "{app}: แจ้งเตือน",
        "toast.heads_up_body": "{label} ใช้ไปแล้ว {pct}%",
        "toast.error_title": "{app} ผิดพลาด",
        "toast.copied": "คัดลอกแล้ว: {text}",
        "toast.eta_warning_title": "{app}: ใกล้เต็มโควต้า",
        "toast.eta_warning_body": "{label} จะเต็มในอีก ~{eta} ที่ rate นี้",
        "toast.battery_paused": "ใช้แบตเตอรี่ — พักการเช็ค",

        # Menu
        "menu.show_status": "ดูสถานะ",
        "menu.show_history": "ดูประวัติ…",
        "menu.refresh_now": "รีเฟรชเดี๋ยวนี้",
        "menu.copy_pct": "คัดลอก % ปัจจุบัน",
        "menu.pause_on_battery": "หยุดเช็คเมื่อใช้แบตเตอรี่",
        "menu.show_sparkline": "แสดง sparkline ใน tooltip",
        "menu.show_cost": "แสดงค่าใช้จ่ายวันนี้ (ccusage)",
        "menu.attribute_window": "เก็บข้อมูลตามหน้าต่างที่ active",
        "menu.show_last_error": "ดู error ล่าสุด",
        "menu.account": "บัญชี",
        "menu.manage_accounts": "จัดการบัญชี…",
        "menu.settings": "ตั้งค่า",
        "menu.alert_thresholds": "เกณฑ์แจ้งเตือน",
        "menu.thresholds_quiet": "เงียบ (95%)",
        "menu.thresholds_default": "ปกติ (80, 95)",
        "menu.thresholds_sensitive": "ละเอียด (50, 75, 90)",
        "menu.thresholds_custom": "กำหนดเอง…",
        "menu.sound_alerts": "เสียงแจ้งเตือน",
        "menu.pause_outside": "หยุดนอกเวลา {start}:00–{end}:00",
        "menu.schedule_settings": "ตั้งค่าตาราง…",
        "menu.icon_theme": "ธีมไอคอน",
        "menu.theme_auto": "อัตโนมัติ",
        "menu.theme_light": "สว่าง",
        "menu.theme_dark": "มืด",
        "menu.icon_style": "รูปแบบไอคอน",
        "menu.style_frame": "กรอบ",
        "menu.style_solid": "ทึบ",
        "menu.style_donut": "โดนัท",
        "menu.style_bar": "แถบ",
        "menu.poll_interval": "ความถี่ในการเช็ค",
        "menu.interval_30s": "30 วินาที",
        "menu.interval_1m": "1 นาที",
        "menu.interval_2m": "2 นาที",
        "menu.interval_5m": "5 นาที",
        "menu.language": "ภาษา",
        "menu.open_console": "เปิด Anthropic Console",
        "menu.console_usage": "Dashboard การใช้งาน",
        "menu.console_limits": "Rate limits",
        "menu.quit": "ออกจาก {app}",

        # Window titles
        "window.history_title": "Claude Quota — {name}",
        "window.status_title": "Claude Quota — {name}",
        "window.current_usage": "การใช้งานปัจจุบัน",
        "window.last_24h": "ย้อนหลัง 24 ชั่วโมง",
        "window.no_history": "ยังไม่มีประวัติ — ปล่อยให้แอปทำงานสักพักก่อน",

        # Plan badge
        "plan.label": "แพคเกจ",
        "plan.unknown": "ไม่ทราบ",

        # Accounts dialog
        "dialog.accounts_title": "Claude Quota — บัญชี",
        "dialog.accounts_heading": "บัญชีที่ตั้งค่าไว้",
        "dialog.accounts_desc": "แต่ละบัญชีจะชี้ไปที่ไฟล์ credentials ของ Claude Code  “Default” จะค้นหา ~/.claude/.credentials.json อัตโนมัติ",
        "dialog.accounts_no_selection": "กรุณาเลือกบัญชีก่อน",
        "dialog.accounts_keep_one": "ต้องมีบัญชีอย่างน้อย 1 รายการ",
        "dialog.accounts_remove_confirm": "ลบ “{name}” ใช่หรือไม่?",
        "dialog.accounts_rename": "เปลี่ยนชื่อ",
        "dialog.accounts_rename_title": "เปลี่ยนชื่อบัญชี",
        "dialog.accounts_rename_label": "ชื่อใหม่:",
        "dialog.accounts_remove_title": "ลบบัญชี",
        "dialog.accounts_add_title": "เพิ่มบัญชี",
        "dialog.accounts_field_name": "ชื่อบัญชี",
        "dialog.accounts_field_mode": "โหมด",
        "dialog.accounts_mode_auto": "ค้นหาอัตโนมัติ",
        "dialog.accounts_mode_file": "เลือกไฟล์ credentials…",
        "dialog.accounts_field_path": "ไฟล์ credentials",
        "dialog.accounts_browse_title": "เลือกไฟล์ credentials.json",
        "dialog.accounts_missing_path": "กรุณาเลือกไฟล์ หรือสลับเป็นค้นหาอัตโนมัติ",

        # Schedule dialog
        "dialog.schedule_title": "Claude Quota — ตารางเวลา",
        "dialog.schedule_heading": "ตารางเวลาการเช็ค",
        "dialog.schedule_desc": "เมื่อเปิด แอปจะหยุดเรียก API นอกเหนือจากช่วงเวลาและวันที่เลือกไว้",
        "dialog.schedule_enable": "เปิดใช้ตารางเวลา",
        "dialog.schedule_hours": "ช่วงเวลาทำงาน:",
        "dialog.schedule_days": "วันที่ทำงาน",

        # Days (short)
        "day.mon": "จ",
        "day.tue": "อ",
        "day.wed": "พ",
        "day.thu": "พฤ",
        "day.fri": "ศ",
        "day.sat": "ส",
        "day.sun": "อา",

        # Thresholds dialog
        "dialog.thresholds_title": "Claude Quota — เกณฑ์แจ้งเตือน",
        "dialog.thresholds_heading": "เกณฑ์การแจ้งเตือน",
        "dialog.thresholds_desc": "ระบบจะแจ้งเตือนเมื่อใช้งานถึงเปอร์เซ็นต์ที่กำหนด\nคั่นด้วยจุลภาค (เช่น 50, 75, 90)",
        "dialog.thresholds_play_sound": "เล่นเสียงเมื่อแจ้งเตือน",
        "dialog.thresholds_invalid": "กรุณาใส่เปอร์เซ็นต์เป็นจำนวนเต็มระหว่าง 1–100",
        "dialog.thresholds_empty": "กรุณาใส่อย่างน้อย 1 ค่า",
    },
}


def _detect_default() -> str:
    try:
        loc = (locale.getdefaultlocale()[0] or "").lower()
    except Exception:
        loc = ""
    return "th" if loc.startswith("th") else "en"


def current_language() -> str:
    lang = user_settings.get("language")
    if not lang or lang not in _T:
        return _detect_default()
    return lang


def set_language(lang: str) -> None:
    if lang not in _T:
        return
    user_settings.update(language=lang)


def t(key: str, **kwargs: Any) -> str:
    """Translate a key. Falls back to English, then to the raw key."""
    lang = current_language()
    text = _T.get(lang, {}).get(key) or _T["en"].get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text
