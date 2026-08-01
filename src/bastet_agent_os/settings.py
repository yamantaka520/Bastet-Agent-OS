"""System settings that belong to the installation, not to a project.

Timezone first, because every timestamp in the UI was UTC. The server keeps
storing UTC — an audit trail in local time is a trail you cannot compare across
machines — and the *display* zone is a setting, applied in the browser.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

# Offered in the picker. Any IANA name is accepted; these are just the ones worth
# one click for this project's users.
COMMON_ZONES = [
    "UTC", "Asia/Taipei", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo",
    "Asia/Seoul", "Asia/Singapore", "Asia/Bangkok", "Asia/Kolkata", "Asia/Dubai",
    "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Sao_Paulo", "Australia/Sydney",
    "Pacific/Auckland",
]

DEFAULT_TIMEZONE = "UTC"


def valid_timezone(name: str) -> bool:
    return bool(name) and name in available_timezones()


def host_timezone() -> str:
    """What the Bastet host itself is set to — the sensible default to offer."""
    try:
        local = datetime.now().astimezone().tzinfo
        name = getattr(local, "key", None) or str(local)
        return name if valid_timezone(name) else DEFAULT_TIMEZONE
    except Exception:
        return DEFAULT_TIMEZONE


def offset_minutes(name: str, at: datetime | None = None) -> int:
    """Current UTC offset in minutes, for showing the picker's effect."""
    try:
        moment = (at or datetime.now(tz=ZoneInfo("UTC"))).astimezone(ZoneInfo(name))
    except Exception:
        return 0
    delta = moment.utcoffset()
    return int(delta.total_seconds() // 60) if delta else 0


def public(cfg: dict) -> dict:
    """The settings payload the WebUI needs. Never includes hosts or secrets."""
    timezone = cfg.get("timezone") or DEFAULT_TIMEZONE
    if not valid_timezone(timezone):
        timezone = DEFAULT_TIMEZONE
    return {
        "timezone": timezone,
        "timezone_offset_minutes": offset_minutes(timezone),
        "host_timezone": host_timezone(),
        "common_timezones": COMMON_ZONES,
    }
