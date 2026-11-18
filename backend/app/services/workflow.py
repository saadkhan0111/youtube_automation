"""Approval workflow and publishing actions. All rules are enforced here, server-side,
regardless of whether the caller is the dashboard, the agent API or the scheduler."""
from __future__ import annotations

import json
import shutil
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..events import record_event
from ..models import (
    PermissionStatus,
    PublishMode,
    RightsReview,
    Upload,
    UploadStatus,
    Video,
    VideoStatus,
)
from ..timeutil import utcnow
from . import oauth, quota, scheduling
from .metadata_rules import ALLOWED_CATEGORIES, clean_hashtags, clean_tags, clean_title, validate_metadata
from .quota import get_schedule
from .youtube_errors import classify_http_error

EDITABLE_STATUSES = {VideoStatus.PENDING_REVIEW, VideoStatus.APPROVED, VideoStatus.REJECTED, VideoStatus.FAILED}


class WorkflowError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise WorkflowError(msg)


def active_upload(db: Session, video: Video) -> Upload | None:
    return db.scalar(select(Upload).where(Upload.video_id == video.id, Upload.upload_status.in_(list(UploadStatus.ACTIVE))))


def successful_upload(db: Session, video: Video) -> Upload | None:
    return db.scalar(select(Upload).where(Upload.video_id == video.id, Upload.youtube_video_id.is_not(None)))


def update_metadata(db: Session, video: Video, data: dict, actor: str) -> Video:
    _require(video.status in EDITABLE_STATUSES, f"Metadata can't be edited while the video is '{video.status}'.")
    if "title" in data and data["title"] is not None:
        video.title = clean_title(data["title"])
    if "description" in data and data["description"] is not None:
        video.description = data["description"].strip()
    if "tags" in data and data["tags"] is not None:
        video.tags = clean_tags(data["tags"])
    if "hashtags" in data and data["hashtags"] is not None:
        video.hashtags = clean_hashtags(data["hashtags"])
    if data.get("category_id"):
        _require(data["category_id"] in ALLOWED_CATEGORIES, "Unsupported category.")
        video.category_id = data["category_id"]
    # Rights record
    for field in ("original_source_url", "source_creator", "permission_notes", "footage_type", "original_commentary"):
        if field in data and data[field] is not None:
            setattr(video, field, data[field])
    if data.get("permission_status"):
        _require(data["permission_status"] in PermissionStatus.ALL, "Invalid permission status.")
        video.permission_status = data["permission_status"]
        if data["permission_status"] not in PermissionStatus.CLEARED:
            video.rights_review_status = RightsReview.PENDING if data["permission_status"] == PermissionStatus.UNKNOWN else RightsReview.BLOCKED
    if data.get("rights_review_status"):
        _require(data["rights_review_status"] in RightsReview.ALL, "Invalid rights review status.")
        _require(actor != "agent", "Only a human can change the rights review status.")
        if data["rights_review_status"] == RightsReview.CLEARED:
            _require(video.permission_status in PermissionStatus.CLEARED,
                     "Set a permission status (original / licensed / authorized / fair use claimed) before clearing rights.")
        video.rights_review_status = data["rights_review_status"]
    video.metadata_source = "manual" if any(k in data for k in ("title", "description", "tags", "hashtags")) else video.metadata_source
    record_event("review", "Metadata/rights updated", video_id=video.id, actor=actor)
    return video


def approve(db: Session, video: Video, actor: str, rights_confirmed: bool, notes: str | None = None) -> Video:
    _require(actor != "agent", "Agents cannot approve videos; approval requires a human.")
    _require(video.status in (VideoStatus.PENDING_REVIEW, VideoStatus.REJECTED), f"Only videos pending review can be approved (status={video.status}).")
    _require(bool(video.validation_ok), "Video failed technical validation: " + "; ".join(json.loads(video.validation_errors_json or "[]")))
    errors = validate_metadata(video)
    _require(not errors, "Metadata problems: " + "; ".join(errors))
    _require(video.permission_status in PermissionStatus.CLEARED,
             "Source permission status must be original, licensed, authorized or fair-use-claimed before approval.")
    _require(rights_confirmed or video.rights_review_status == RightsReview.CLEARED,
             "Confirm that you reviewed the source rights and that the clip adds original value.")
    video.rights_review_status = RightsReview.CLEARED
    video.status = VideoStatus.APPROVED
    video.reviewed_at = utcnow()
    if notes:
        video.review_notes = notes
    record_event("review", "Video approved", video_id=video.id, actor=actor)
    return video


def reject(db: Session, video: Video, actor: str, reason: str | None) -> Video:
    _require(video.status in (VideoStatus.PENDING_REVIEW, VideoStatus.APPROVED, VideoStatus.FAILED),
             f"Can't reject a video with status '{video.status}'.")
    _require(active_upload(db, video) is None, "Cancel the queued upload first.")
    video.status = VideoStatus.REJECTED
    video.reviewed_at = utcnow()
    video.review_notes = reason or video.review_notes
    record_event("review", f"Video rejected: {reason or ''}", video_id=video.id, actor=actor)
    return video


def delete(db: Session, video: Video, actor: str) -> None:
    _require(actor != "agent", "Agents cannot delete videos.")
    _require(active_upload(db, video) is None, "Cancel the active upload before deleting.")
    _require(successful_upload(db, video) is None,
             "This clip was already uploaded to YouTube. Delete it in YouTube Studio; the local record is kept for duplicate protection.")
    trash = settings.data_dir / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    try:
        from pathlib import Path

        p = Path(video.file_path)
        if p.exists():
            shutil.move(str(p), str(trash / p.name))
    except Exception:
        pass
    for up in list(video.uploads):
        db.delete(up)
    db.delete(video)
    record_event("review", f"Video #{video.id} deleted (file moved to data/trash)", actor=actor)


def enqueue_upload(db: Session, video: Video, mode: str, publish_at: datetime | None, actor: str) -> Upload:
    sched = get_schedule(db)
    _require(mode in PublishMode.ALL, "Invalid publish mode.")
    _require(video.reviewed_at is not None and video.status in (VideoStatus.APPROVED, VideoStatus.FAILED),
             f"Only approved videos can be uploaded (status={video.status}).")
    _require(video.rights_review_status == RightsReview.CLEARED and video.permission_status in PermissionStatus.CLEARED,
             "Source rights are not cleared.")
    _require(bool(video.validation_ok), "Video failed technical validation.")
    _require(active_upload(db, video) is None, "This video already has an upload in progress or queued.")
    existing = successful_upload(db, video)
    _require(existing is None, f"Already uploaded as {existing.youtube_video_id if existing else ''}; duplicate uploads are blocked.")
    errors = validate_metadata(video)
    _require(not errors, "Metadata problems: " + "; ".join(errors))
    if mode in (PublishMode.SCHEDULED, PublishMode.PUBLIC):
        _require(sched.allow_public_publishing,
                 "Public publishing (scheduled or publish-now) is disabled. Enable it in Settings once you're ready.")
    if mode == PublishMode.SCHEDULED:
        if publish_at is None:
            slots = scheduling.next_free_slots(db, sched, 1)
            _require(bool(slots), "No free publishing slot found.")
            publish_at = slots[0]
        scheduling.validate_publish_time(db, sched, publish_at)
    acct = oauth.active_account(db)
    up = Upload(
        video_id=video.id, account_id=acct.id if acct else None, publish_mode=mode,
        privacy_status="private" if mode == PublishMode.SCHEDULED else mode if mode != PublishMode.PUBLIC else "public",
        scheduled_at=publish_at if mode == PublishMode.SCHEDULED else None,
        upload_status=UploadStatus.QUEUED, requested_by=actor,
    )
    db.add(up)
    video.status = VideoStatus.QUEUED
    db.flush()
    record_event("upload", f"Queued {mode} upload" + (f" for {publish_at.isoformat()}Z" if publish_at and mode == PublishMode.SCHEDULED else ""),
                 video_id=video.id, upload_id=up.id, actor=actor)
    return up


def retry_upload(db: Session, up: Upload, actor: str) -> Upload:
    _require(up.upload_status in (UploadStatus.FAILED, UploadStatus.PAUSED, UploadStatus.QUEUED),
             f"Upload is '{up.upload_status}' and can't be retried.")
    _require(up.youtube_video_id is None, "Upload already succeeded.")
    video = db.get(Video, up.video_id)
    if up.publish_mode == PublishMode.SCHEDULED and up.scheduled_at:
        scheduling.validate_publish_time(db, get_schedule(db), up.scheduled_at, exclude_upload_id=up.id)
    up.upload_status = UploadStatus.QUEUED if not up.resumable_uri else UploadStatus.PAUSED
    up.last_error = None
    up.error_kind = None
    video.status = VideoStatus.QUEUED
    record_event("upload", "Retry requested", video_id=video.id, upload_id=up.id, actor=actor)
    return up


def reschedule(db: Session, up: Upload, new_time: datetime, actor: str) -> Upload:
    sched = get_schedule(db)
    _require(up.publish_mode == PublishMode.SCHEDULED, "Only scheduled uploads can be rescheduled.")
    scheduling.validate_publish_time(db, sched, new_time, exclude_upload_id=up.id)
    video = db.get(Video, up.video_id)
    if up.youtube_video_id is None:
        _require(up.upload_status in (UploadStatus.QUEUED, UploadStatus.PAUSED, UploadStatus.FAILED),
                 "Upload is in progress; try again when it finishes.")
        up.scheduled_at = new_time
        record_event("schedule", f"Rescheduled (before upload) to {new_time.isoformat()}Z", video_id=video.id, upload_id=up.id, actor=actor)
        return up
    # Already on YouTube: needs videos.update (youtube.force-ssl scope, 50 units).
    _require(video.status == VideoStatus.SCHEDULED, "Video is no longer scheduled (it may already be published).")
    acct = oauth.active_account(db)
    _require(acct is not None and "youtube.force-ssl" in (acct.granted_scopes or ""),
             "Rescheduling an uploaded video needs the youtube.force-ssl scope. Set YOUTUBE_ENABLE_MANAGE_SCOPE=true and reconnect, or change the time in YouTube Studio.")
    from ..timeutil import to_rfc3339
    from googleapiclient.errors import HttpError

    service = oauth.build_service(oauth.get_credentials(acct))
    log_id = quota.begin_request("videos.update", upload_id=up.id, detail="reschedule publishAt")
    try:
        service.videos().update(part="status", body={
            "id": up.youtube_video_id,
            "status": {"privacyStatus": "private", "publishAt": to_rfc3339(new_time), "selfDeclaredMadeForKids": False, "embeddable": True},
        }).execute()
    except HttpError as exc:
        info = classify_http_error(exc)
        quota.finish_request(log_id, False, info.status, info.reason, info.message)
        raise WorkflowError(f"YouTube rejected the reschedule: {info.message}") from exc
    quota.finish_request(log_id, True, 200)
    up.scheduled_at = new_time
    record_event("schedule", f"Rescheduled on YouTube to {new_time.isoformat()}Z", video_id=video.id, upload_id=up.id, actor=actor)
    return up


def auto_schedule(db: Session, horizon: int = 3) -> list[int]:
    """Optional automatic mode: assign already-approved, rights-cleared clips to free slots."""
    sched = get_schedule(db)
    if not sched.enabled:
        return []
    if not sched.allow_public_publishing:
        record_event("schedule", "Auto-scheduling is enabled but public publishing is disabled; nothing scheduled.", level="warning", actor="auto")
        return []
    candidates = list(db.scalars(
        select(Video).where(
            Video.status == VideoStatus.APPROVED,
            Video.reviewed_at.is_not(None),
            Video.rights_review_status == RightsReview.CLEARED,
            Video.validation_ok.is_(True),
        ).order_by(Video.reviewed_at)
    ))
    created = []
    for video in candidates[:horizon]:
        slots = scheduling.next_free_slots(db, sched, 1)
        if not slots:
            break
        try:
            up = enqueue_upload(db, video, PublishMode.SCHEDULED, slots[0], actor="auto")
            created.append(up.id)
        except (WorkflowError, scheduling.ScheduleError) as exc:
            record_event("schedule", f"Auto-schedule skipped video #{video.id}: {exc}", level="warning", video_id=video.id, actor="auto")
    return created
