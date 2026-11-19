"""Background jobs (APScheduler)."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .config import QUOTA_TIMEZONE, settings
from .db import session_scope
from .events import record_event
from .models import PublishMode, Upload, UploadStatus, Video, VideoStatus, YouTubeAccount
from .services import ingest, quota, uploader, workflow
from .timeutil import utcnow

log = logging.getLogger("ytshorts.jobs")
scheduler: BackgroundScheduler | None = None
_queue_lock = threading.Lock()


def recover_on_startup() -> None:
    n = quota.reconcile_pending_requests()
    if n:
        record_event("startup", f"{n} API request(s) were in flight when the app stopped; quota flagged as uncertain.", level="warning")
    with session_scope() as s:
        for up in s.scalars(select(Upload).where(Upload.upload_status == UploadStatus.UPLOADING)):
            up.upload_status = UploadStatus.PAUSED
            up.error_kind = "network"
            up.last_error = "Application stopped during upload; will resume the saved session."
            record_event("startup", "Interrupted upload will be resumed", upload_id=up.id, video_id=up.video_id)


def next_upload_id() -> int | None:
    """Oldest upload that can run now."""
    with session_scope() as s:
        acct_ok = s.scalar(select(YouTubeAccount).where(YouTubeAccount.is_active.is_(True), YouTubeAccount.connection_status == "connected")) is not None
        rows = list(s.scalars(
            select(Upload).where(Upload.upload_status.in_([UploadStatus.QUEUED, UploadStatus.PAUSED])).order_by(Upload.id)
        ))
        for up in rows:
            if up.cancel_requested:
                continue
            if up.error_kind == "auth" and not acct_ok:
                continue
            if not acct_ok:
                continue
            return up.id
    return None


def process_queue() -> None:
    if not _queue_lock.acquire(blocking=False):
        return
    try:
        for _ in range(5):  # at most a few per tick
            up_id = next_upload_id()
            if up_id is None:
                return
            with session_scope() as s:
                resumable = bool(s.get(Upload, up_id).resumable_uri)
            if not resumable:
                try:
                    quota.check_allowed("videos.insert")
                except quota.QuotaBlocked as exc:
                    log.info("upload queue paused: %s", exc.reason)
                    return
            outcome = uploader.run_upload(up_id)
            log.info("upload %s -> %s %s", up_id, outcome.status, outcome.error or "")
            if outcome.status not in (UploadStatus.SUCCEEDED, UploadStatus.FAILED, UploadStatus.CANCELLED):
                return  # paused/quota/auth: stop this tick, try again later
    finally:
        _queue_lock.release()


def run_auto_schedule() -> None:
    with session_scope() as s:
        created = workflow.auto_schedule(s)
    if created:
        trigger_queue()


def sync_scheduled() -> None:
    """After publishAt passes, confirm on YouTube that the video went public."""
    with session_scope() as s:
        ids = [up.id for up in s.scalars(select(Upload).join(Video).where(
            Upload.publish_mode == PublishMode.SCHEDULED,
            Upload.upload_status == UploadStatus.SUCCEEDED,
            Upload.scheduled_at < utcnow(),
            Video.status == VideoStatus.SCHEDULED,
        ))]
    for up_id in ids:
        try:
            quota.check_allowed("videos.list")
            uploader.verify_uploaded(up_id)
        except quota.QuotaBlocked:
            return
        except Exception as exc:
            log.warning("sync of upload %s failed: %s", up_id, exc)


def quota_reset_job() -> None:
    record_event("quota", "Daily quota counters reset (midnight Pacific Time).")


def trigger_queue() -> None:
    if scheduler is not None:
        scheduler.add_job(process_queue, id="process_queue_now", replace_existing=True)
    else:
        threading.Thread(target=process_queue, daemon=True).start()


def start() -> None:
    global scheduler
    recover_on_startup()
    from .clipper import worker as clip_worker

    clip_worker.recover_on_startup()
    from .clipper import live as clip_live

    clip_live.recover_on_startup()
    from .studio import worker as studio_worker

    studio_worker.recover_on_startup()
    if not settings.scheduler_enabled:
        return
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    scheduler.add_job(ingest.scan_incoming, "interval", seconds=settings.ingest_interval_seconds, id="ingest")
    scheduler.add_job(process_queue, "interval", seconds=60, id="process_queue")
    scheduler.add_job(run_auto_schedule, "interval", minutes=10, id="auto_schedule")
    scheduler.add_job(sync_scheduled, "interval", minutes=30, id="sync_scheduled")
    scheduler.add_job(quota_reset_job, CronTrigger(hour=0, minute=0, timezone=QUOTA_TIMEZONE), id="quota_reset")
    scheduler.start()
    log.info("background scheduler started")


def shutdown() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
