"""Dashboard REST API."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..events import record_event
from ..models import ApiRequestLog, EventLog, PublishMode, Upload, UploadStatus, Video, VideoStatus
from ..security import SESSION_COOKIE, check_login, session_value
from ..serializers import iso, upload_dict, video_dict
from ..services import ingest, metadata, oauth, quota, scheduling, uploader, workflow
from ..services.paths import ensure_in_library
from ..services.quota import get_schedule
from ..services.youtube_errors import AuthError
from ..timeutil import parse_local, utc_to_local, utcnow

router = APIRouter(prefix="/api")
# Commit before the response is sent (so the next request sees the change).
DBDep = Depends(get_db, scope="function")
ACTOR = "dashboard"


def _tz(db: Session) -> str:
    return get_schedule(db).timezone


def _video(db: Session, video_id: int) -> Video:
    v = db.get(Video, video_id)
    if v is None:
        raise HTTPException(404, "Video not found")
    return v


def _wf(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (workflow.WorkflowError, scheduling.ScheduleError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


# ---------------------------------------------------------------- misc
@router.get("/health")
def health():
    return {"ok": True}


class LoginBody(BaseModel):
    token: str


@router.post("/login")
def login(body: LoginBody, response: Response):
    check_login(body.token)
    if settings.dashboard_token:
        response.set_cookie(SESSION_COOKIE, session_value(), httponly=True, samesite="strict", max_age=30 * 86400)
    return {"ok": True}


# ---------------------------------------------------------------- auth
@router.get("/auth/status")
def auth_status(db: Session = DBDep):
    return oauth.connection_status(db)


@router.post("/auth/start")
def auth_start():
    try:
        return {"auth_url": oauth.start_authorization()}
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/auth/callback")
def auth_callback(state: str | None = None, code: str | None = None, error: str | None = None):
    base = settings.dashboard_url.rstrip("/") + "/#/settings"
    try:
        oauth.complete_authorization(state, code, error)
        return RedirectResponse(base + "?oauth=connected")
    except AuthError as exc:
        from urllib.parse import quote

        return RedirectResponse(base + "?oauth=error&message=" + quote(str(exc)[:300]))


@router.post("/auth/verify")
def auth_verify(db: Session = DBDep):
    """Refresh the token if needed and re-check the channel (1 quota unit)."""
    try:
        ch = oauth.verify_connection(db)
        return {"ok": True, "channel": {"id": ch["id"], "name": ch["title"]}}
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except quota.QuotaBlocked as exc:
        raise HTTPException(429, exc.reason) from exc
    except ConnectionError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/auth/disconnect")
def auth_disconnect(db: Session = DBDep):
    oauth.disconnect(db)
    return {"ok": True}


# ---------------------------------------------------------------- overview
@router.get("/overview")
def overview(db: Session = DBDep):
    counts = dict(db.execute(select(Video.status, func.count()).group_by(Video.status)).all())
    total = sum(counts.values())
    failed_uploads = db.scalar(select(func.count()).select_from(Upload).where(Upload.upload_status == UploadStatus.FAILED))
    tz = _tz(db)
    upcoming = db.scalars(
        select(Upload).where(Upload.publish_mode == PublishMode.SCHEDULED, Upload.scheduled_at > utcnow(),
                             Upload.upload_status.in_(list(UploadStatus.ACTIVE) + [UploadStatus.SUCCEEDED]))
        .order_by(Upload.scheduled_at).limit(5)
    ).all()
    recent_events = db.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(15)).all()
    return {
        "auth": oauth.connection_status(db),
        "counts": {s: counts.get(s, 0) for s in VideoStatus.ALL},
        "total_clips": total,
        "pending_approvals": counts.get(VideoStatus.PENDING_REVIEW, 0),
        "uploaded": counts.get(VideoStatus.UPLOADED, 0) + counts.get(VideoStatus.SCHEDULED, 0) + counts.get(VideoStatus.PUBLISHED, 0),
        "scheduled": counts.get(VideoStatus.SCHEDULED, 0),
        "failed_uploads": failed_uploads,
        "quota": quota.status(db),
        "upcoming": [{**upload_dict(u, tz), "title": u.video.title} for u in upcoming],
        "events": [{"timestamp": iso(e.timestamp), "level": e.level, "category": e.category, "message": e.message,
                    "video_id": e.video_id, "upload_id": e.upload_id} for e in recent_events],
        "timezone": tz,
    }


# ---------------------------------------------------------------- videos
@router.get("/videos")
def list_videos(status: str | None = None, db: Session = DBDep):
    q = select(Video).order_by(Video.id.desc())
    if status:
        q = q.where(Video.status.in_(status.split(",")))
    tz = _tz(db)
    return [video_dict(v, tz) for v in db.scalars(q)]


@router.get("/videos/{video_id}")
def get_video(video_id: int, db: Session = DBDep):
    return video_dict(_video(db, video_id), _tz(db))


@router.get("/videos/{video_id}/file")
def video_file(video_id: int, db: Session = DBDep):
    v = _video(db, video_id)
    try:
        path = ensure_in_library(Path(v.file_path))
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "File missing")
    return FileResponse(path, media_type="video/mp4")


class VideoPatch(BaseModel):
    title: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] | None = None
    hashtags: list[str] | None = None
    category_id: str | None = None
    original_source_url: str | None = None
    source_creator: str | None = None
    permission_status: str | None = None
    permission_notes: str | None = None
    footage_type: str | None = None
    original_commentary: str | None = None
    rights_review_status: str | None = None


@router.patch("/videos/{video_id}")
def patch_video(video_id: int, body: VideoPatch, db: Session = DBDep):
    v = _video(db, video_id)
    _wf(workflow.update_metadata, db, v, body.model_dump(exclude_unset=True), ACTOR)
    db.flush()
    return video_dict(v, _tz(db))


@router.post("/videos/{video_id}/generate-metadata")
def regenerate_metadata(video_id: int, db: Session = DBDep):
    v = _video(db, video_id)
    if v.status not in workflow.EDITABLE_STATUSES:
        raise HTTPException(400, "Metadata can't be changed in this state.")
    result = metadata.generate_metadata(db, v, actor=ACTOR)
    db.flush()
    return {"result": result, "video": video_dict(v, _tz(db))}


class ApproveBody(BaseModel):
    rights_confirmed: bool = False
    notes: str | None = None


@router.post("/videos/{video_id}/approve")
def approve_video(video_id: int, body: ApproveBody, db: Session = DBDep):
    v = _video(db, video_id)
    _wf(workflow.approve, db, v, ACTOR, body.rights_confirmed, body.notes)
    return video_dict(v, _tz(db))


class RejectBody(BaseModel):
    reason: str | None = None


@router.post("/videos/{video_id}/reject")
def reject_video(video_id: int, body: RejectBody, db: Session = DBDep):
    v = _video(db, video_id)
    _wf(workflow.reject, db, v, ACTOR, body.reason)
    return video_dict(v, _tz(db))


@router.delete("/videos/{video_id}")
def delete_video(video_id: int, db: Session = DBDep):
    v = _video(db, video_id)
    _wf(workflow.delete, db, v, ACTOR)
    return {"ok": True}


class UploadBody(BaseModel):
    mode: Literal["private", "unlisted", "scheduled", "public"]
    # ISO datetime. Without an offset it's interpreted in the schedule timezone (Asia/Karachi).
    publish_at: str | None = None


@router.post("/videos/{video_id}/upload")
def upload_video(video_id: int, body: UploadBody, db: Session = DBDep):
    from ..jobs import trigger_queue

    v = _video(db, video_id)
    when = None
    if body.publish_at:
        try:
            when = parse_local(body.publish_at, _tz(db))
        except ValueError as exc:
            raise HTTPException(400, f"Invalid publish time: {exc}") from exc
    up = _wf(workflow.enqueue_upload, db, v, body.mode, when, ACTOR)
    db.commit()
    trigger_queue()
    return upload_dict(up, _tz(db))


@router.post("/videos/import")
def import_video(
    file: UploadFile = File(...),
    title: str = Form(""),
    description: str = Form(""),
    topic: str = Form(""),
    context_notes: str = Form(""),
    original_commentary: str = Form(""),
    source_url: str = Form(""),
    source_creator: str = Form(""),
    permission_status: str = Form("unknown"),
    permission_notes: str = Form(""),
    footage_type: str = Form(""),
    clip_start: str = Form(""),
    clip_end: str = Form(""),
):
    """Upload an MP4 from the dashboard. It is validated and lands in 'pending review'."""
    from ..models import PermissionStatus

    if not (file.filename or "").lower().endswith(".mp4"):
        raise HTTPException(400, "Only .mp4 files are supported.")
    if permission_status not in PermissionStatus.ALL:
        raise HTTPException(400, "Invalid permission status.")
    sidecar = {
        "title": title.strip() or None, "description": description.strip() or None, "topic": topic.strip() or None,
        "context_notes": context_notes.strip() or None, "original_commentary": original_commentary.strip() or None,
        "source_url": source_url.strip() or None, "source_creator": source_creator.strip() or None,
        "permission_status": permission_status, "permission_notes": permission_notes.strip() or None,
        "footage_type": footage_type or None, "clip_start": clip_start or None, "clip_end": clip_end or None,
    }
    try:
        vid = ingest.save_uploaded_clip(file.file, file.filename, sidecar)
    except ingest.DuplicateClip as exc:
        raise HTTPException(409, f"This exact file was already added as video #{exc.existing_id}.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": vid}


@router.post("/ingest/scan")
def scan_now():
    ids = ingest.scan_incoming()
    return {"imported": ids, "incoming_dir": str(settings.incoming_dir)}


# ---------------------------------------------------------------- uploads
@router.get("/uploads")
def list_uploads(db: Session = DBDep):
    tz = _tz(db)
    return [{**upload_dict(u, tz), "title": u.video.title} for u in db.scalars(select(Upload).order_by(Upload.id.desc()).limit(200))]


def _upload(db: Session, upload_id: int) -> Upload:
    up = db.get(Upload, upload_id)
    if up is None:
        raise HTTPException(404, "Upload not found")
    return up


@router.post("/uploads/{upload_id}/cancel")
def cancel_upload(upload_id: int, db: Session = DBDep):
    _upload(db, upload_id)
    uploader.request_cancel(upload_id)
    return {"ok": True}


@router.post("/uploads/{upload_id}/retry")
def retry(upload_id: int, db: Session = DBDep):
    from ..jobs import trigger_queue

    up = _upload(db, upload_id)
    _wf(workflow.retry_upload, db, up, ACTOR)
    db.commit()
    trigger_queue()
    return upload_dict(up, _tz(db))


class RescheduleBody(BaseModel):
    publish_at: str


@router.post("/uploads/{upload_id}/reschedule")
def reschedule(upload_id: int, body: RescheduleBody, db: Session = DBDep):
    up = _upload(db, upload_id)
    try:
        when = parse_local(body.publish_at, _tz(db))
    except ValueError as exc:
        raise HTTPException(400, f"Invalid publish time: {exc}") from exc
    _wf(workflow.reschedule, db, up, when, ACTOR)
    return upload_dict(up, _tz(db))


@router.post("/uploads/{upload_id}/verify")
def verify(upload_id: int, db: Session = DBDep):
    up = _upload(db, upload_id)
    if not up.youtube_video_id:
        raise HTTPException(400, "Not uploaded yet")
    try:
        return uploader.verify_uploaded(upload_id)
    except quota.QuotaBlocked as exc:
        raise HTTPException(429, exc.reason) from exc
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


# ---------------------------------------------------------------- calendar
@router.get("/calendar")
def calendar(days: int = Query(30, ge=1, le=180), db: Session = DBDep):
    sched = get_schedule(db)
    tz = sched.timezone
    start = utcnow() - timedelta(days=7)
    end = utcnow() + timedelta(days=days)
    ups = db.scalars(select(Upload).where(Upload.publish_mode == PublishMode.SCHEDULED, Upload.scheduled_at.between(start, end),
                                          Upload.upload_status.notin_([UploadStatus.CANCELLED])).order_by(Upload.scheduled_at)).all()
    free = scheduling.next_free_slots(db, sched, count=min(days * sched.daily_upload_limit, 30))
    return {
        "timezone": tz,
        "entries": [{**upload_dict(u, tz), "title": u.video.title, "video_status": u.video.status} for u in ups],
        "free_slots": [{"utc": iso(f), "local": utc_to_local(f, tz).isoformat()} for f in free if f <= end],
    }


# ---------------------------------------------------------------- quota
@router.get("/quota")
def quota_status(db: Session = DBDep):
    return quota.status(db)


@router.get("/quota/log")
def quota_log(limit: int = Query(100, le=1000), db: Session = DBDep):
    rows = db.scalars(select(ApiRequestLog).order_by(ApiRequestLog.id.desc()).limit(limit)).all()
    return [{"id": r.id, "timestamp": iso(r.timestamp), "quota_date": r.quota_date.isoformat(), "method": r.method,
             "bucket": r.quota_type, "estimated_cost": r.estimated_cost, "result": r.result, "http_status": r.http_status,
             "error_reason": r.error_reason, "is_retry": r.is_retry, "upload_id": r.upload_id} for r in rows]


@router.post("/quota/clear-uncertain")
def quota_clear():
    quota.clear_uncertain(actor=ACTOR)
    return {"ok": True}


# ---------------------------------------------------------------- settings
class SettingsBody(BaseModel):
    enabled: bool | None = None
    timezone: str | None = None
    daily_upload_limit: int | None = Field(None, ge=1, le=20)
    publishing_times: list[str] | None = None
    approval_required: bool | None = None
    default_privacy: Literal["private", "unlisted"] | None = None
    allow_public_publishing: bool | None = None
    min_lead_minutes: int | None = Field(None, ge=15, le=1440)
    max_daily_upload_calls: int | None = Field(None, ge=0, le=100)
    max_daily_search_calls: int | None = Field(None, ge=0, le=100)
    max_daily_other_quota_units: int | None = Field(None, ge=0, le=10000)


def _settings_dict(s) -> dict:
    return {
        "enabled": s.enabled, "timezone": s.timezone, "daily_upload_limit": s.daily_upload_limit,
        "publishing_times": s.publishing_times, "approval_required": s.approval_required,
        "default_privacy": s.default_privacy, "allow_public_publishing": s.allow_public_publishing,
        "min_lead_minutes": s.min_lead_minutes, "max_daily_upload_calls": s.max_daily_upload_calls,
        "max_daily_search_calls": s.max_daily_search_calls, "max_daily_other_quota_units": s.max_daily_other_quota_units,
        "ai_metadata": {"enabled": settings.metadata_ai_enabled, "model": settings.anthropic_model,
                        "api_key_present": bool(__import__("os").environ.get("ANTHROPIC_API_KEY"))},
        "incoming_dir": str(settings.incoming_dir),
        "library_dir": str(settings.library_dir),
        "ffprobe_available": __import__("app.services.validation", fromlist=["x"]).ffprobe_available(),
        "agent_api_enabled": bool(settings.agent_api_token),
    }


@router.get("/settings")
def get_settings(db: Session = DBDep):
    return _settings_dict(get_schedule(db))


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = DBDep):
    from zoneinfo import ZoneInfo

    s = get_schedule(db)
    data = body.model_dump(exclude_unset=True)
    if "timezone" in data:
        try:
            ZoneInfo(data["timezone"])
        except Exception as exc:
            raise HTTPException(400, "Unknown timezone") from exc
    if "publishing_times" in data:
        try:
            data["publishing_times"] = scheduling.normalize_times(data["publishing_times"])
        except scheduling.ScheduleError as exc:
            raise HTTPException(400, str(exc)) from exc
    if data.get("approval_required") is False:
        raise HTTPException(400, "Human approval can't be disabled: only approved videos may be uploaded.")
    for k, v in data.items():
        setattr(s, k, v)
    record_event("settings", f"Settings updated: {', '.join(sorted(data))}", actor=ACTOR)
    if data.get("allow_public_publishing"):
        record_event("settings", "PUBLIC PUBLISHING ENABLED by user", level="warning", actor=ACTOR)
    return _settings_dict(s)


@router.get("/events")
def events(limit: int = Query(100, le=500), db: Session = DBDep):
    rows = db.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(limit)).all()
    return [{"timestamp": iso(e.timestamp), "level": e.level, "category": e.category, "message": e.message,
             "video_id": e.video_id, "upload_id": e.upload_id, "actor": e.actor} for e in rows]
