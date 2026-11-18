"""Publishing slots and publish-time validation (Asia/Karachi by default)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PublishMode, PublishingSchedule, Upload, UploadStatus
from ..timeutil import to_naive_utc, utc_to_local, utcnow

MAX_SCHEDULE_DAYS_AHEAD = 180


class ScheduleError(ValueError):
    pass


def _parse_hhmm(s: str) -> time:
    try:
        h, m = s.strip().split(":")
        return time(int(h), int(m))
    except Exception as exc:
        raise ScheduleError(f"Invalid time '{s}'. Use HH:MM (24h).") from exc


def normalize_times(times: list[str]) -> list[str]:
    parsed = sorted({_parse_hhmm(t) for t in times})
    if not parsed:
        raise ScheduleError("At least one publishing time is required.")
    return [t.strftime("%H:%M") for t in parsed]


def _taken_slots(db: Session) -> list[datetime]:
    q = select(Upload.scheduled_at).where(
        Upload.publish_mode == PublishMode.SCHEDULED,
        Upload.scheduled_at.is_not(None),
        Upload.upload_status.in_(list(UploadStatus.ACTIVE) + [UploadStatus.SUCCEEDED]),
    )
    return [d for d in db.scalars(q) if d]


def count_on_local_day(db: Session, day_local, tz: str, exclude_upload_id: int | None = None) -> int:
    n = 0
    q = select(Upload).where(
        Upload.publish_mode == PublishMode.SCHEDULED,
        Upload.scheduled_at.is_not(None),
        Upload.upload_status.in_(list(UploadStatus.ACTIVE) + [UploadStatus.SUCCEEDED]),
    )
    for up in db.scalars(q):
        if up.id != exclude_upload_id and utc_to_local(up.scheduled_at, tz).date() == day_local:
            n += 1
    return n


def next_free_slots(db: Session, sched: PublishingSchedule, count: int = 1, after: datetime | None = None,
                    extra_taken: list[datetime] | None = None) -> list[datetime]:
    """Next `count` free publishing slots (naive UTC) honoring times and daily limit."""
    tz = ZoneInfo(sched.timezone)
    earliest = max(after or utcnow(), utcnow() + timedelta(minutes=sched.min_lead_minutes))
    times = [_parse_hhmm(t) for t in sched.publishing_times][: max(1, sched.daily_upload_limit)]
    taken = _taken_slots(db) + list(extra_taken or [])
    result: list[datetime] = []
    day = utc_to_local(earliest, sched.timezone).date()
    for _ in range(MAX_SCHEDULE_DAYS_AHEAD):
        day_taken = [t for t in taken if utc_to_local(t, sched.timezone).date() == day]
        used = len(day_taken)
        for t in times:
            if used >= sched.daily_upload_limit:
                break
            slot = to_naive_utc(datetime.combine(day, t, tzinfo=tz))
            if slot < earliest:
                continue
            if any(abs((slot - x).total_seconds()) < 60 for x in day_taken):
                continue
            result.append(slot)
            taken.append(slot)
            day_taken.append(slot)
            used += 1
            if len(result) >= count:
                return result
        day += timedelta(days=1)
    return result


def validate_publish_time(db: Session, sched: PublishingSchedule, when_utc: datetime, exclude_upload_id: int | None = None) -> None:
    now = utcnow()
    if when_utc <= now:
        raise ScheduleError("Publish time is in the past. YouTube may publish a past publishAt immediately, so it is refused.")
    if when_utc < now + timedelta(minutes=sched.min_lead_minutes):
        raise ScheduleError(f"Publish time must be at least {sched.min_lead_minutes} minutes in the future (upload + processing time).")
    if when_utc > now + timedelta(days=MAX_SCHEDULE_DAYS_AHEAD):
        raise ScheduleError(f"Publish time must be within {MAX_SCHEDULE_DAYS_AHEAD} days.")
    local_day = utc_to_local(when_utc, sched.timezone).date()
    if count_on_local_day(db, local_day, sched.timezone, exclude_upload_id) >= sched.daily_upload_limit:
        raise ScheduleError(f"Daily publishing limit ({sched.daily_upload_limit}) already reached on {local_day.isoformat()}.")
