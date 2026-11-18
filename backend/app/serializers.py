"""Convert models to API dicts. Never includes tokens or secrets."""
from __future__ import annotations

import json

from .models import Upload, Video
from .timeutil import utc_to_local


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def local(dt, tz):
    return utc_to_local(dt, tz).isoformat() if dt else None


def upload_dict(up: Upload, tz: str) -> dict:
    return {
        "id": up.id,
        "video_id": up.video_id,
        "youtube_video_id": up.youtube_video_id,
        "youtube_url": f"https://youtube.com/shorts/{up.youtube_video_id}" if up.youtube_video_id else None,
        "studio_url": f"https://studio.youtube.com/video/{up.youtube_video_id}/edit" if up.youtube_video_id else None,
        "upload_status": up.upload_status,
        "publish_mode": up.publish_mode,
        "privacy_status": up.privacy_status,
        "scheduled_at": iso(up.scheduled_at),
        "scheduled_at_local": local(up.scheduled_at, tz),
        "published_at": iso(up.published_at),
        "bytes_uploaded": up.bytes_uploaded,
        "total_bytes": up.total_bytes,
        "progress": round(up.bytes_uploaded / up.total_bytes, 4) if up.total_bytes else 0,
        "resumable_session": bool(up.resumable_uri),
        "upload_attempts": up.upload_attempts,
        "insert_calls": up.insert_calls,
        "last_error": up.last_error,
        "error_kind": up.error_kind,
        "requested_by": up.requested_by,
        "created_at": iso(up.created_at),
        "updated_at": iso(up.updated_at),
    }


def video_dict(v: Video, tz: str, include_uploads: bool = True) -> dict:
    d = {
        "id": v.id,
        "status": v.status,
        "title": v.title,
        "description": v.description,
        "tags": v.tags,
        "hashtags": v.hashtags,
        "category_id": v.category_id,
        "metadata_source": v.metadata_source,
        "suggested_publish_at": iso(v.suggested_publish_at),
        "suggested_publish_at_local": local(v.suggested_publish_at, tz),
        "source": {
            "original_source_url": v.original_source_url,
            "source_creator": v.source_creator,
            "permission_status": v.permission_status,
            "permission_notes": v.permission_notes,
            "footage_type": v.footage_type,
            "rights_review_status": v.rights_review_status,
            "original_commentary": v.original_commentary,
        },
        "clip_start_time": v.clip_start_time,
        "clip_end_time": v.clip_end_time,
        "topic": v.topic,
        "context_notes": v.context_notes,
        "has_transcript": bool(v.transcript),
        "file_name": v.file_path.replace("\\", "/").rsplit("/", 1)[-1] if v.file_path else None,
        "preview_url": f"/api/videos/{v.id}/file",
        "duration": v.duration,
        "resolution": v.resolution,
        "width": v.width,
        "height": v.height,
        "video_codec": v.video_codec,
        "audio_codec": v.audio_codec,
        "frame_rate": v.frame_rate,
        "file_size": v.file_size,
        "validation_ok": v.validation_ok,
        "validation_errors": json.loads(v.validation_errors_json or "[]"),
        "validation_warnings": json.loads(v.validation_warnings_json or "[]"),
        "reviewed_at": iso(v.reviewed_at),
        "review_notes": v.review_notes,
        "created_at": iso(v.created_at),
        "updated_at": iso(v.updated_at),
    }
    if include_uploads:
        d["uploads"] = [upload_dict(u, tz) for u in v.uploads]
    return d
