"""Timezone helpers. All datetimes stored in the DB are naive UTC."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import QUOTA_TIMEZONE

PACIFIC = ZoneInfo(QUOTA_TIMEZONE)


def utcnow() -> datetime:
    """Naive UTC 'now' (DB convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    return to_aware_utc(dt).replace(tzinfo=None)


def to_rfc3339(dt: datetime) -> str:
    """ISO 8601 / RFC 3339 UTC timestamp as required by status.publishAt."""
    return to_aware_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_local(dt_str: str, tz_name: str) -> datetime:
    """Parse an ISO datetime. If it has no offset, interpret it in tz_name. Returns naive UTC."""
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return to_naive_utc(dt)


def utc_to_local(dt: datetime, tz_name: str) -> datetime:
    return to_aware_utc(dt).astimezone(ZoneInfo(tz_name))


def quota_day(now: datetime | None = None) -> date:
    """The YouTube quota day (Pacific calendar date) for a UTC instant."""
    now = now or utcnow()
    return to_aware_utc(now).astimezone(PACIFIC).date()


def last_quota_reset(now: datetime | None = None) -> datetime:
    """Most recent midnight Pacific, as naive UTC."""
    d = quota_day(now)
    return to_naive_utc(datetime.combine(d, time(0, 0), tzinfo=PACIFIC))


def next_quota_reset(now: datetime | None = None) -> datetime:
    d = quota_day(now) + timedelta(days=1)
    return to_naive_utc(datetime.combine(d, time(0, 0), tzinfo=PACIFIC))
