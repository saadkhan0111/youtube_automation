"""YouTube Data API quota tracking.

Google's documented defaults (subject to change):
  * videos.insert: separate "Video Uploads" bucket, 100 calls/day, 1 unit per call
  * search.list:   separate bucket, 100 calls/day
  * everything else: shared 10,000 units/day
  * reset: midnight Pacific Time

This module enforces *application safety limits* (configurable, lower than
Google's), records every request, and fails closed: if the local record cannot
be read or is flagged as uncertain/exhausted, new API work is blocked.
Counters are incremented *before* a request is sent, so a crash mid-request
over-counts rather than under-counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..events import record_event
from ..models import ApiRequestLog, PublishingSchedule, QuotaUsage
from ..timeutil import last_quota_reset, next_quota_reset, quota_day, utcnow

UPLOAD, SEARCH, OTHER = "upload", "search", "other"

# method -> (bucket, estimated cost in that bucket's unit)
# upload/search buckets count calls; "other" counts units.
QUOTA_COSTS: dict[str, tuple[str, int]] = {
    "videos.insert": (UPLOAD, 1),
    "search.list": (SEARCH, 1),
    "channels.list": (OTHER, 1),
    "videos.list": (OTHER, 1),
    "videos.update": (OTHER, 50),
    "videos.delete": (OTHER, 50),
    "thumbnails.set": (OTHER, 50),
}

# Google's documented default allowances (for display only; not enforced as truth).
GOOGLE_DEFAULTS = {UPLOAD: 100, SEARCH: 100, OTHER: 10000}


class QuotaBlocked(Exception):
    """Raised when a request must not be sent because of quota limits or uncertainty."""

    def __init__(self, bucket: str, reason: str):
        super().__init__(reason)
        self.bucket = bucket
        self.reason = reason


@dataclass
class BucketStatus:
    bucket: str
    used: int
    limit: int
    google_default: int
    requests: int
    failed: int
    retries: int
    successful_uploads: int
    exhausted: bool
    uncertain: bool
    note: str | None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "google_default_allowance": self.google_default,
            "google_remaining_estimate": max(0, self.google_default - self.used),
            "requests": self.requests,
            "failed_requests": self.failed,
            "retry_attempts": self.retries,
            "successful_uploads": self.successful_uploads,
            "exhausted": self.exhausted,
            "uncertain": self.uncertain,
            "note": self.note,
            "unit": "calls" if self.bucket in (UPLOAD, SEARCH) else "units",
        }


def get_schedule(db: Session) -> PublishingSchedule:
    sched = db.scalar(select(PublishingSchedule).limit(1))
    if sched is None:
        sched = PublishingSchedule()
        db.add(sched)
        db.flush()
    return sched


def _limits(db: Session) -> dict[str, int]:
    s = get_schedule(db)
    return {UPLOAD: s.max_daily_upload_calls, SEARCH: s.max_daily_search_calls, OTHER: s.max_daily_other_quota_units}


def _usage_row(db: Session, day: date, bucket: str) -> QuotaUsage:
    row = db.scalar(select(QuotaUsage).where(QuotaUsage.quota_date == day, QuotaUsage.quota_type == bucket))
    if row is None:
        row = QuotaUsage(quota_date=day, quota_type=bucket, requests_used=0, units_used=0)
        db.add(row)
        db.flush()
    return row


def bucket_for(method: str) -> tuple[str, int]:
    if method not in QUOTA_COSTS:
        # Unknown method: assume an expensive call rather than a cheap one.
        return (OTHER, 50)
    return QUOTA_COSTS[method]


def check_allowed(method: str, db: Session | None = None) -> None:
    """Raise QuotaBlocked if `method` must not be called right now."""
    bucket, cost = bucket_for(method)
    try:
        if db is None:
            with session_scope() as s:
                _check(s, bucket, cost)
        else:
            _check(db, bucket, cost)
    except QuotaBlocked:
        raise
    except Exception as exc:  # fail closed
        raise QuotaBlocked(bucket, f"Quota record could not be read ({exc}); refusing to call the API.") from exc


def _check(db: Session, bucket: str, cost: int) -> None:
    day = quota_day()
    row = _usage_row(db, day, bucket)
    limit = _limits(db)[bucket]
    if row.exhausted:
        raise QuotaBlocked(bucket, f"Google reported the {bucket} quota as exhausted today. Waiting for the midnight Pacific reset.")
    if row.uncertain:
        raise QuotaBlocked(bucket, f"Quota usage for the {bucket} bucket is uncertain ({row.note or 'unknown'}). Review and clear the warning in the dashboard before more automated calls.")
    if row.units_used + cost > limit:
        raise QuotaBlocked(bucket, f"Daily {bucket} safety limit reached ({row.units_used}/{limit}).")


def begin_request(method: str, is_retry: bool = False, upload_id: int | None = None, detail: str | None = None) -> int:
    """Check limits, count the request, and log it as pending. Returns the log id."""
    bucket, cost = bucket_for(method)
    with session_scope() as s:
        _check(s, bucket, cost)
        day = quota_day()
        row = _usage_row(s, day, bucket)
        row.requests_used += 1
        row.units_used += cost
        if is_retry:
            row.retry_attempts += 1
        log = ApiRequestLog(
            quota_date=day, method=method, quota_type=bucket, estimated_cost=cost,
            is_retry=is_retry, upload_id=upload_id, detail=detail, result="pending",
        )
        s.add(log)
        s.flush()
        return log.id


def finish_request(log_id: int, success: bool, http_status: int | None = None, error_reason: str | None = None,
                   detail: str | None = None, upload_succeeded: bool = False) -> None:
    with session_scope() as s:
        log = s.get(ApiRequestLog, log_id)
        if log is None:
            return
        log.result = "success" if success else "error"
        log.http_status = http_status
        log.error_reason = error_reason
        if detail:
            log.detail = (detail or "")[:2000]
        row = _usage_row(s, log.quota_date, log.quota_type)
        if not success:
            row.failed_requests += 1
        if upload_succeeded:
            row.successful_uploads += 1


def record_upload_success() -> None:
    """Count a successful upload that completed in a resumed session (no open log row)."""
    with session_scope() as s:
        _usage_row(s, quota_day(), UPLOAD).successful_uploads += 1


def mark_exhausted(bucket: str, reason: str) -> None:
    with session_scope() as s:
        row = _usage_row(s, quota_day(), bucket)
        row.exhausted = True
        row.note = reason[:500]
    record_event("quota", f"{bucket} quota marked exhausted: {reason}", level="warning")


def mark_uncertain(bucket: str, reason: str) -> None:
    with session_scope() as s:
        row = _usage_row(s, quota_day(), bucket)
        row.uncertain = True
        row.note = reason[:500]
    record_event("quota", f"{bucket} quota marked uncertain: {reason}", level="warning")


def clear_uncertain(actor: str = "dashboard") -> None:
    with session_scope() as s:
        for row in s.scalars(select(QuotaUsage).where(QuotaUsage.quota_date == quota_day(), QuotaUsage.uncertain.is_(True))):
            row.uncertain = False
            row.note = (row.note or "") + " [cleared by user]"
    record_event("quota", "Quota uncertainty warning cleared", actor=actor)


def reconcile_pending_requests() -> int:
    """On startup: requests left 'pending' mean the app stopped mid-call.
    They are already counted, but their outcome is unknown -> flag as uncertain."""
    with session_scope() as s:
        pending = list(s.scalars(select(ApiRequestLog).where(ApiRequestLog.result == "pending")))
        buckets = set()
        for log in pending:
            log.result = "unknown"
            if log.quota_date == quota_day():
                buckets.add(log.quota_type)
    for b in buckets:
        mark_uncertain(b, "The application stopped while an API request was in flight; its outcome is unknown.")
    return len(pending)


def status(db: Session) -> dict:
    day = quota_day()
    limits = _limits(db)
    buckets = {}
    for bucket in (UPLOAD, SEARCH, OTHER):
        row = _usage_row(db, day, bucket)
        buckets[bucket] = BucketStatus(
            bucket=bucket, used=row.units_used, limit=limits[bucket], google_default=GOOGLE_DEFAULTS[bucket],
            requests=row.requests_used, failed=row.failed_requests, retries=row.retry_attempts,
            successful_uploads=row.successful_uploads, exhausted=row.exhausted, uncertain=row.uncertain, note=row.note,
        ).as_dict()
    warnings = []
    for b in buckets.values():
        if b["exhausted"]:
            warnings.append(f"{b['bucket']}: Google reported quota exhausted; automated calls paused until reset.")
        elif b["uncertain"]:
            warnings.append(f"{b['bucket']}: usage is uncertain; automated calls paused until you review it.")
        elif b["remaining"] == 0:
            warnings.append(f"{b['bucket']}: daily safety limit reached.")
    return {
        "quota_day_pacific": day.isoformat(),
        "last_reset_utc": last_quota_reset().isoformat() + "Z",
        "next_reset_utc": next_quota_reset().isoformat() + "Z",
        "buckets": buckets,
        "uploads_allowed": not (buckets[UPLOAD]["exhausted"] or buckets[UPLOAD]["uncertain"] or buckets[UPLOAD]["remaining"] == 0),
        "warnings": warnings,
        "disclaimer": "Limits shown are this app's safety limits. Google's actual quotas may differ and can change; check Google Cloud Console > APIs & Services > Quotas for authoritative numbers.",
        "now_utc": utcnow().isoformat() + "Z",
    }
