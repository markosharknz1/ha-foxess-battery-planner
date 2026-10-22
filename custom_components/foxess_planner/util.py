"""Small helpers shared by the config flow and controller (no HA imports)."""
from __future__ import annotations

import re


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return re.sub(r"^[0-9_]+", "", slug)


def valid_slug(slug: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*", slug or ""))


def time_to_minutes(value: str) -> int:
    """'02:00' or '02:00:00' -> minutes since midnight."""
    parts = str(value).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def timezones_agree(zone_a: str, zone_b: str, now_utc) -> bool | None:
    """True if two IANA zones have the same UTC offset now AND half a year on
    (so zones with different DST rules do not pass). None if either is unknown."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        a, b = ZoneInfo(zone_a), ZoneInfo(zone_b)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    for moment in (now_utc, now_utc + timedelta(days=182)):
        if moment.astimezone(a).utcoffset() != moment.astimezone(b).utcoffset():
            return False
    return True


def minutes_apart(a: int, b: int) -> int:
    """Shortest distance between two minute-of-day values, wrapping midnight."""
    diff = abs(a - b) % 1440
    return min(diff, 1440 - diff)
