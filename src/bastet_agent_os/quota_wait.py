"""Vendor quota failures carry their own retry time — read it.

The live case: every attempt died in seconds with `You've hit your session
limit · resets 1:30am (Asia/Taipei)`, the card blocked, and a human had to
notice, wait for the vendor's clock, and press retry at the right moment. An
orchestrator exists precisely so nobody has to do that: a quota failure is not
an error to investigate but a timer to wait out, and the message usually states
the deadline.

`parse_reset()` recognises the failure and extracts the reset time when one is
stated; the orchestrator then parks the job with a `resume_at` and a background
loop retries it when the clock passes. When the message names no time, a
default backoff applies — waiting 30 minutes beats waiting for a person.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

# what counts as "the vendor said no for now, not the task failing"
QUOTA_MARKERS = (
    "session limit", "usage limit", "rate limit", "quota exceeded",
    "hit your limit", "limit reached", "too many requests", "overloaded",
    "capacity", "credit balance is too low",
)

# "resets 1:30am (Asia/Taipei)" · "resets at 3pm" · "resets 17:30 (UTC)"
_RESET_RE = re.compile(
    r"reset(?:s|s at| at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE)

DEFAULT_BACKOFF_MIN = 30
SAFETY_MARGIN_MIN = 3          # vendor clocks and ours disagree by a little
MAX_WAIT_HOURS = 26            # beyond this, the parse is probably wrong


def is_quota_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in QUOTA_MARKERS)


def parse_reset(text: str, now: datetime | None = None) -> str | None:
    """When a quota failure lifts, as UTC ISO — or None if `text` is not a
    quota failure at all. A quota failure with no parseable time gets the
    default backoff; a stated time gets the next occurrence of that wall-clock
    time in the stated zone, plus a safety margin."""
    if not is_quota_failure(text):
        return None
    moment = now or datetime.now(UTC)
    match = _RESET_RE.search(text or "")
    if not match:
        return (moment + timedelta(minutes=DEFAULT_BACKOFF_MIN)).isoformat(
            timespec="seconds")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    zone_name = (match.group(4) or "").strip()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return (moment + timedelta(minutes=DEFAULT_BACKOFF_MIN)).isoformat(
            timespec="seconds")
    zone = UTC
    if zone_name and zone_name in available_timezones():
        zone = ZoneInfo(zone_name)
    local = moment.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)     # that time already passed today
    candidate += timedelta(minutes=SAFETY_MARGIN_MIN)
    if candidate - local > timedelta(hours=MAX_WAIT_HOURS):
        candidate = local + timedelta(minutes=DEFAULT_BACKOFF_MIN)
    return candidate.astimezone(UTC).isoformat(timespec="seconds")
