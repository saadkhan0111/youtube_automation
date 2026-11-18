"""YouTube upload engine (videos.insert, resumable).

Reliability rules:
* Only approved, rights-cleared, validated videos inside the library directory are uploaded.
* A videos.insert call is counted against quota *before* it is sent.
* The resumable session URI is persisted after it is obtained and after every
  chunk. After a network failure or an app restart the existing session is
  resumed (status query + continue) instead of starting a new videos.insert.
* Temporary errors (network, 429, 500/502/503/504) are retried with exponential
  backoff. Permanent errors are not retried automatically.
* A failed upload is never marked as successful.
"""
from __future__ import annotations

import logging
import random
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httplib2
from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..events import record_event
from ..models import (
    PermissionStatus,
    PublishMode,
    RightsReview,
    Upload,
    UploadStatus,
    Video,
    VideoStatus,
    YouTubeAccount,
)
from ..timeutil import to_rfc3339, utcnow
from . import oauth, quota
from .metadata_rules import build_upload_body
from .paths import ensure_in_library
from .validation import validate_video
from .youtube_errors import AuthError, classify_http_error

log = logging.getLogger("ytshorts.uploader")

NETWORK_ERRORS = (httplib2.HttpLib2Error, ConnectionError, TimeoutError, socket.timeout, OSError)

# Injectable for tests
sleep = time.sleep

_cancel_events: dict[int, threading.Event] = {}
_upload_lock = threading.Lock()


class UploadRejected(Exception):
    """The upload must not proceed (gate failed). Not retried automatically."""


@dataclass
class UploadOutcome:
    upload_id: int
    status: str
    youtube_video_id: str | None = None
    error: str | None = None
    error_kind: str | None = None


def request_cancel(upload_id: int) -> None:
    ev = _cancel_events.get(upload_id)
    if ev:
        ev.set()
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        if up and up.upload_status in UploadStatus.ACTIVE:
            up.cancel_requested = True
            if up.upload_status in (UploadStatus.QUEUED, UploadStatus.PAUSED):
                # Not currently transferring: cancel immediately.
                up.upload_status = UploadStatus.CANCELLED
                up.resumable_uri = None
                v = s.get(Video, up.video_id)
                if v and v.status in (VideoStatus.QUEUED, VideoStatus.UPLOADING, VideoStatus.FAILED):
                    v.status = VideoStatus.APPROVED
    record_event("upload", "Cancellation requested", upload_id=upload_id)


def _update(upload_id: int, **fields) -> None:
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        for k, v in fields.items():
            setattr(up, k, v)


def _set_video_status(video_id: int, status: str) -> None:
    with session_scope() as s:
        v = s.get(Video, video_id)
        if v:
            v.status = status


def check_upload_gate(s, up: Upload, video: Video) -> None:
    """Every rule that must hold before any byte is sent. Raises UploadRejected."""
    from .quota import get_schedule

    sched = get_schedule(s)
    if video.reviewed_at is None or video.status not in (VideoStatus.QUEUED, VideoStatus.UPLOADING):
        raise UploadRejected(f"Video {video.id} is not approved for upload (status={video.status}).")
    if video.rights_review_status != RightsReview.CLEARED or video.permission_status not in PermissionStatus.CLEARED:
        raise UploadRejected("Source rights have not been cleared for this video.")
    if not video.title or not video.title.strip():
        raise UploadRejected("Video has no title.")
    # Duplicate protection: never upload the same video (or identical file) twice.
    dup = s.scalar(
        select(Upload).join(Video).where(
            Upload.youtube_video_id.is_not(None),
            Upload.id != up.id,
            (Upload.video_id == video.id) | ((Video.file_sha256 == video.file_sha256) & Video.file_sha256.is_not(None)),
        )
    )
    if dup is not None:
        raise UploadRejected(f"Duplicate: this video/file was already uploaded as YouTube video {dup.youtube_video_id}.")
    if up.publish_mode in (PublishMode.SCHEDULED, PublishMode.PUBLIC) and not sched.allow_public_publishing:
        raise UploadRejected("Public/scheduled publishing is disabled in Settings. Only private or unlisted uploads are allowed.")
    if up.publish_mode == PublishMode.SCHEDULED:
        if up.scheduled_at is None:
            raise UploadRejected("Scheduled upload has no publication time.")
        lead = (up.scheduled_at - utcnow()).total_seconds() / 60
        if lead < max(15, sched.min_lead_minutes // 2):
            raise UploadRejected(
                f"Scheduled time {to_rfc3339(up.scheduled_at)} is too close or in the past "
                "(YouTube may publish immediately). Reschedule it."
            )


def run_upload(upload_id: int) -> UploadOutcome:
    """Run (or resume) one upload synchronously. Only one upload runs at a time."""
    if not _upload_lock.acquire(blocking=False):
        return UploadOutcome(upload_id, "busy", error="Another upload is in progress.")
    cancel = _cancel_events.setdefault(upload_id, threading.Event())
    try:
        return _run(upload_id, cancel)
    finally:
        _cancel_events.pop(upload_id, None)
        _upload_lock.release()


def _fail(upload_id: int, video_id: int, kind: str, message: str, final: bool, keep_uri: bool = True) -> UploadOutcome:
    """final=True -> FAILED (needs user action). final=False -> PAUSED/QUEUED for automatic recovery."""
    if final:
        status = UploadStatus.FAILED
    elif kind == "quota":
        status = UploadStatus.QUEUED
    else:
        status = UploadStatus.PAUSED
    fields = dict(upload_status=status, last_error=message[:2000], error_kind=kind)
    if not keep_uri:
        fields["resumable_uri"] = None
        fields["bytes_uploaded"] = 0
    _update(upload_id, **fields)
    with session_scope() as s:
        v = s.get(Video, video_id)
        # Only touch videos that were in the upload pipeline (never overwrite e.g. pending_review).
        if v and v.status in (VideoStatus.QUEUED, VideoStatus.UPLOADING, VideoStatus.FAILED):
            v.status = VideoStatus.FAILED if final else VideoStatus.QUEUED
    record_event("upload", f"Upload {'failed' if final else 'paused'} ({kind}): {message}",
                 level="error" if final else "warning", video_id=video_id, upload_id=upload_id)
    return UploadOutcome(upload_id, status, error=message, error_kind=kind)


def _run(upload_id: int, cancel: threading.Event) -> UploadOutcome:
    # ---------- load + gate
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        if up is None:
            return UploadOutcome(upload_id, "missing", error="Upload not found")
        if up.upload_status not in UploadStatus.ACTIVE:
            return UploadOutcome(upload_id, up.upload_status, up.youtube_video_id, error="Upload is not active")
        if up.youtube_video_id:
            return UploadOutcome(upload_id, UploadStatus.SUCCEEDED, up.youtube_video_id)
        video = s.get(Video, up.video_id)
        video_id = video.id
        try:
            check_upload_gate(s, up, video)
            file_path = ensure_in_library(Path(video.file_path))
        except (UploadRejected, ValueError) as exc:
            s.rollback()
            return _fail(upload_id, video_id, "permanent", str(exc), final=True, keep_uri=False)
        account = s.get(YouTubeAccount, up.account_id) if up.account_id else oauth.active_account(s)
        if account is None or account.connection_status != "connected":
            s.rollback()
            return _fail(upload_id, video_id, "auth", "No connected YouTube channel. Connect/reconnect in Settings.", final=False)
        up.account_id = account.id
        body = build_upload_body(video, up)
        resumable_uri = up.resumable_uri
        up.upload_status = UploadStatus.UPLOADING
        up.upload_attempts += 1
        up.cancel_requested = False
        up.last_error = None
        video.status = VideoStatus.UPLOADING
        s.flush()
        s.expunge(account)

    # ---------- validate the actual file right before upload
    vres = validate_video(file_path)
    if not vres.ok:
        return _fail(upload_id, video_id, "validation", "Video failed validation: " + "; ".join(vres.errors), final=True, keep_uri=False)

    # ---------- credentials
    try:
        creds = oauth.get_credentials(account)
    except AuthError as exc:
        return _fail(upload_id, video_id, "auth", str(exc), final=False)
    except ConnectionError as exc:
        return _fail(upload_id, video_id, "network", str(exc), final=False)
    service = oauth.build_service(creds)

    media = MediaFileUpload(str(file_path), mimetype="video/mp4", chunksize=settings.upload_chunk_size_mb * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    total = file_path.stat().st_size
    _update(upload_id, total_bytes=total)

    if resumable_uri:
        # Resume the existing session: query the server for the committed byte range first.
        request.resumable_uri = resumable_uri
        request._in_error_state = True
        record_event("upload", "Resuming existing resumable upload session", video_id=video_id, upload_id=upload_id)

    insert_log_id: int | None = None
    retries = 0
    response = None
    initiations = 0

    while response is None:
        if cancel.is_set() or _cancel_flag(upload_id):
            _update(upload_id, upload_status=UploadStatus.CANCELLED, resumable_uri=None, last_error="Cancelled by user")
            _set_video_status(video_id, VideoStatus.APPROVED)
            if insert_log_id:
                quota.finish_request(insert_log_id, False, None, "cancelled")
            record_event("upload", "Upload cancelled", video_id=video_id, upload_id=upload_id)
            return UploadOutcome(upload_id, UploadStatus.CANCELLED)

        if request.resumable_uri is None:
            # This next_chunk() will initiate a new videos.insert -> counts against the upload quota.
            try:
                if insert_log_id:
                    quota.finish_request(insert_log_id, False, None, "re-initiated")
                insert_log_id = quota.begin_request("videos.insert", is_retry=initiations > 0, upload_id=upload_id)
            except quota.QuotaBlocked as exc:
                return _fail(upload_id, video_id, "quota", exc.reason, final=False, keep_uri=False)
            initiations += 1
            _update(upload_id, insert_calls=_get(upload_id).insert_calls + 1)

        try:
            status, response = request.next_chunk()
            retries = 0
            if request.resumable_uri:
                _update(upload_id, resumable_uri=request.resumable_uri, bytes_uploaded=int(request.resumable_progress or 0))
            if status is not None:
                log.info("upload %s: %.1f%%", upload_id, status.progress() * 100)
        except HttpError as exc:
            info = classify_http_error(exc)
            if request.resumable_uri:
                _update(upload_id, resumable_uri=request.resumable_uri)
            if info.status in (404, 410) and request.resumable_uri:
                # Upload session expired/unknown: a new videos.insert is required.
                record_event("upload", "Resumable session expired; a new upload session will be started",
                             level="warning", video_id=video_id, upload_id=upload_id)
                request.resumable_uri = None
                request.resumable_progress = 0
                _update(upload_id, resumable_uri=None, bytes_uploaded=0)
                retries += 1
                if retries > settings.upload_max_retries:
                    return _finish_error(insert_log_id, upload_id, video_id, "network", "Upload session repeatedly expired.", final=False)
                continue
            if info.kind == "quota":
                quota.mark_exhausted(quota.UPLOAD, f"{info.reason}: {info.message}")
                return _finish_error(insert_log_id, upload_id, video_id, "quota", f"YouTube quota exceeded ({info.reason}). Will retry after the quota reset.", final=False, status=info.status, reason=info.reason, keep_uri=bool(request.resumable_uri))
            if info.kind == "auth":
                oauth._set_status(account.id, "expired", info.message)
                return _finish_error(insert_log_id, upload_id, video_id, "auth", f"Authentication error: {info.message}", final=False, status=info.status, reason=info.reason)
            if info.retryable:
                retries += 1
                if retries > settings.upload_max_retries:
                    return _finish_error(insert_log_id, upload_id, video_id, "network", f"Gave up after {retries - 1} retries: {info.message}", final=False, status=info.status, reason=info.reason)
                _backoff(retries, upload_id, f"HTTP {info.status} {info.reason or ''}")
                continue
            return _finish_error(insert_log_id, upload_id, video_id, "permanent", f"YouTube rejected the upload: HTTP {info.status} {info.reason}: {info.message}", final=True, status=info.status, reason=info.reason, keep_uri=False)
        except ResumableUploadError as exc:
            info = classify_http_error(exc)
            if info.kind == "quota":
                quota.mark_exhausted(quota.UPLOAD, f"{info.reason}: {info.message}")
                return _finish_error(insert_log_id, upload_id, video_id, "quota", f"YouTube quota exceeded ({info.reason}).", final=False, status=info.status, reason=info.reason, keep_uri=False)
            if info.retryable:
                retries += 1
                if retries > settings.upload_max_retries:
                    return _finish_error(insert_log_id, upload_id, video_id, "network", f"Could not start upload session: {info.message}", final=False)
                _backoff(retries, upload_id, "session start failed")
                continue
            return _finish_error(insert_log_id, upload_id, video_id, "permanent" if info.kind != "auth" else "auth",
                                 f"Could not start upload session: HTTP {info.status} {info.reason}: {info.message}",
                                 final=info.kind != "auth", status=info.status, reason=info.reason, keep_uri=False)
        except NETWORK_ERRORS as exc:
            if request.resumable_uri:
                request._in_error_state = True  # query committed range before sending more bytes
                _update(upload_id, resumable_uri=request.resumable_uri)
            retries += 1
            if retries > settings.upload_max_retries:
                return _finish_error(insert_log_id, upload_id, video_id, "network",
                                     f"Network interrupted ({exc.__class__.__name__}); upload paused and will resume from the saved session.", final=False)
            _backoff(retries, upload_id, exc.__class__.__name__)
            continue

    # ---------- success
    yt_id = response.get("id") if isinstance(response, dict) else None
    if not yt_id:
        return _finish_error(insert_log_id, upload_id, video_id, "permanent", "Upload finished but YouTube returned no video ID.", final=True)
    if insert_log_id:
        quota.finish_request(insert_log_id, True, 200, upload_succeeded=True)
    else:
        quota.record_upload_success()

    with session_scope() as s:
        up = s.get(Upload, upload_id)
        up.youtube_video_id = yt_id
        up.upload_status = UploadStatus.SUCCEEDED
        up.resumable_uri = None
        up.bytes_uploaded = total
        up.last_error = None
        up.error_kind = None
        st = response.get("status", {})
        up.privacy_status = st.get("privacyStatus", up.privacy_status)
        v = s.get(Video, up.video_id)
        if up.publish_mode == PublishMode.SCHEDULED:
            v.status = VideoStatus.SCHEDULED
        elif up.publish_mode == PublishMode.PUBLIC:
            v.status = VideoStatus.PUBLISHED
            up.published_at = utcnow()
        else:
            v.status = VideoStatus.UPLOADED
    record_event("upload", f"Uploaded as YouTube video {yt_id}", video_id=video_id, upload_id=upload_id)

    # ---------- verify what YouTube stored (1 unit)
    try:
        verify_uploaded(upload_id, service)
    except Exception as exc:  # verification problems never undo a confirmed upload
        record_event("upload", f"Post-upload verification skipped: {exc}", level="warning", video_id=video_id, upload_id=upload_id)
    return UploadOutcome(upload_id, UploadStatus.SUCCEEDED, yt_id)


def _get(upload_id: int) -> Upload:
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        s.expunge(up)
        return up


def _cancel_flag(upload_id: int) -> bool:
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        return bool(up and up.cancel_requested)


def _backoff(attempt: int, upload_id: int, why: str) -> None:
    delay = min(64.0, (2 ** attempt) + random.random())
    log.warning("upload %s: temporary error (%s); retry %s in %.1fs", upload_id, why, attempt, delay)
    if attempt == 1:
        record_event("upload", f"Temporary error ({why}); retrying with backoff", level="warning", upload_id=upload_id)
    sleep(delay)


def _finish_error(insert_log_id, upload_id, video_id, kind, message, final, status=None, reason=None, keep_uri=True) -> UploadOutcome:
    if insert_log_id:
        quota.finish_request(insert_log_id, False, status, reason or kind, message)
    return _fail(upload_id, video_id, kind, message, final=final, keep_uri=keep_uri)


def verify_uploaded(upload_id: int, service=None) -> dict:
    """videos.list on the uploaded video: confirm privacy status / publishAt / processing state."""
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        yt_id, account_id, mode, scheduled_at = up.youtube_video_id, up.account_id, up.publish_mode, up.scheduled_at
    if not yt_id:
        raise ValueError("Upload has no YouTube video ID")
    if service is None:
        with session_scope() as s:
            acct = s.get(YouTubeAccount, account_id)
            s.expunge(acct)
        service = oauth.build_service(oauth.get_credentials(acct))
    log_id = quota.begin_request("videos.list", upload_id=upload_id, detail=f"id={yt_id}")
    try:
        resp = service.videos().list(part="status,snippet,processingDetails", id=yt_id).execute()
    except HttpError as exc:
        info = classify_http_error(exc)
        quota.finish_request(log_id, False, info.status, info.reason, info.message)
        raise
    quota.finish_request(log_id, True, 200)
    items = resp.get("items") or []
    if not items:
        return {"found": False}
    st = items[0].get("status", {})
    result = {
        "found": True,
        "privacyStatus": st.get("privacyStatus"),
        "publishAt": st.get("publishAt"),
        "uploadStatus": st.get("uploadStatus"),
        "rejectionReason": st.get("rejectionReason"),
        "failureReason": st.get("failureReason"),
        "title": items[0].get("snippet", {}).get("title"),
    }
    with session_scope() as s:
        up = s.get(Upload, upload_id)
        v = s.get(Video, up.video_id)
        up.privacy_status = st.get("privacyStatus", up.privacy_status)
        if st.get("uploadStatus") in ("rejected", "failed"):
            up.last_error = f"YouTube processing {st.get('uploadStatus')}: {st.get('rejectionReason') or st.get('failureReason')}"
            v.status = VideoStatus.FAILED
        elif mode == PublishMode.SCHEDULED and st.get("privacyStatus") == "public":
            v.status = VideoStatus.PUBLISHED
            up.published_at = up.published_at or utcnow()
        if mode == PublishMode.SCHEDULED and scheduled_at and not st.get("publishAt") and st.get("privacyStatus") != "public":
            up.last_error = "Warning: YouTube did not report a publishAt for this scheduled video. Check YouTube Studio."
    return result
