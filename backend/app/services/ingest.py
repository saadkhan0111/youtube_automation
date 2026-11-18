"""Watch-folder ingest: picks up finished clips from the clipping engine.

Contract with the clipper (see README):
  incoming/<name>.mp4   the rendered clip
  incoming/<name>.json  sidecar, written LAST (its presence means "render complete")

Accepted clips are moved into the library directory and registered with
status=pending_review. Nothing is ever uploaded from the incoming folder.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..events import record_event
from ..models import PermissionStatus, Video, VideoStatus
from . import metadata, scheduling
from .quota import get_schedule
from .validation import file_is_stable, validate_video

log = logging.getLogger("ytshorts.ingest")

SIDECAR_FIELDS = {
    "source_url", "source_creator", "permission_status", "permission_notes", "footage_type",
    "clip_start", "clip_end", "context_notes", "transcript", "topic", "original_commentary",
    "title", "description", "tags", "hashtags",
}


class DuplicateClip(Exception):
    def __init__(self, existing_id: int):
        super().__init__(f"Identical file already imported as video #{existing_id}")
        self.existing_id = existing_id


def save_uploaded_clip(stream, filename: str, sidecar_data: dict, generate: bool = True) -> int:
    """Store a clip uploaded through the dashboard and import it immediately.

    The file is written under a temporary name and only renamed to .mp4 once
    fully received, so the folder scanner never sees a partial file."""
    import re

    settings.incoming_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename or "clip").stem)[:60] or "clip"
    stem = f"upload_{uuid.uuid4().hex[:6]}_{stem}"
    tmp = settings.incoming_dir / f"{stem}.uploading"
    mp4 = settings.incoming_dir / f"{stem}.mp4"
    sidecar = settings.incoming_dir / f"{stem}.json"
    try:
        with tmp.open("wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)
        if tmp.stat().st_size == 0:
            raise ValueError("Uploaded file is empty.")
        tmp.rename(mp4)
        sidecar.write_text(json.dumps(sidecar_data), encoding="utf-8")
        vid = import_clip(mp4, sidecar, generate=generate, check_stable=False)
        if vid is None:
            raise ValueError("Clip could not be imported.")
        return vid
    except DuplicateClip:
        raise
    except Exception:
        for p in (tmp, mp4, sidecar):
            p.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _move_aside(paths: list[Path], sub: str, reason: str) -> None:
    dest = settings.incoming_dir / sub
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.exists():
            shutil.move(str(p), str(dest / p.name))
    (dest / (paths[0].stem + ".reason.txt")).write_text(reason, encoding="utf-8")


def _float(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def scan_incoming(generate: bool = True) -> list[int]:
    """Import every ready clip. Returns new video IDs."""
    settings.incoming_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for sidecar in sorted(settings.incoming_dir.glob("*.json")):
        mp4 = sidecar.with_suffix(".mp4")
        if not mp4.exists():
            continue
        try:
            vid = import_clip(mp4, sidecar, generate=generate)
            if vid:
                created.append(vid)
        except DuplicateClip:
            continue  # already moved to incoming/duplicates
        except Exception as exc:
            log.exception("ingest failed for %s", mp4.name)
            record_event("ingest", f"Failed to import {mp4.name}: {exc}", level="error")
            try:
                _move_aside([mp4, sidecar], "rejected", f"Import error: {exc}")
            except Exception:
                pass
    return created


def import_clip(mp4: Path, sidecar: Path, generate: bool = True, check_stable: bool = True) -> int | None:
    if check_stable and (not file_is_stable(mp4) or not file_is_stable(sidecar, stable_seconds=2)):
        return None  # still being written; try next scan
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("sidecar must be a JSON object")
    except Exception as exc:
        _move_aside([mp4, sidecar], "rejected", f"Invalid sidecar JSON: {exc}")
        record_event("ingest", f"Rejected {mp4.name}: invalid sidecar JSON", level="error")
        return None
    if meta.get("render_complete") is False:
        return None

    digest = sha256_file(mp4)
    with session_scope() as s:
        dup = s.scalar(select(Video).where(Video.file_sha256 == digest))
        if dup:
            _move_aside([mp4, sidecar], "duplicates", f"Identical file already imported as video #{dup.id}")
            record_event("ingest", f"Skipped duplicate clip {mp4.name} (same as video #{dup.id})", level="warning")
            raise DuplicateClip(dup.id)

    vres = validate_video(mp4, check_stable=False)

    # Move into the library (uploads are only allowed from there).
    sub = settings.library_dir / datetime.now().strftime("%Y-%m")
    sub.mkdir(parents=True, exist_ok=True)
    base = f"{uuid.uuid4().hex[:8]}_{mp4.stem}"[:120]
    dest_mp4 = sub / f"{base}.mp4"
    shutil.move(str(mp4), str(dest_mp4))
    shutil.move(str(sidecar), str(sub / f"{base}.json"))

    perm = meta.get("permission_status", PermissionStatus.UNKNOWN)
    if perm not in PermissionStatus.ALL:
        perm = PermissionStatus.UNKNOWN

    with session_scope() as s:
        v = Video(
            original_source_url=meta.get("source_url"),
            source_creator=meta.get("source_creator"),
            permission_status=perm,
            permission_notes=meta.get("permission_notes"),
            footage_type=meta.get("footage_type"),
            original_commentary=meta.get("original_commentary"),
            clip_start_time=_float(meta.get("clip_start")),
            clip_end_time=_float(meta.get("clip_end")),
            file_path=str(dest_mp4),
            file_sha256=digest,
            file_size=vres.file_size,
            context_notes=meta.get("context_notes"),
            transcript=meta.get("transcript"),
            topic=meta.get("topic"),
            duration=vres.duration, width=vres.width, height=vres.height,
            video_codec=vres.video_codec, audio_codec=vres.audio_codec, frame_rate=vres.frame_rate,
            validation_ok=vres.ok,
            status=VideoStatus.PENDING_REVIEW,
        )
        v.validation_errors_json = json.dumps(vres.errors)
        v.validation_warnings_json = json.dumps(vres.warnings)
        s.add(v)
        s.flush()
        if meta.get("title"):
            # Clipper supplied metadata: keep it (reviewer can still regenerate).
            v.title = metadata.clean_title(meta.get("title"))
            v.description = meta.get("description") or ""
            v.tags = metadata.clean_tags(meta.get("tags") or [])
            v.hashtags = metadata.clean_hashtags(meta.get("hashtags") or [])
            v.metadata_source = "clipper"
        elif generate:
            try:
                metadata.generate_metadata(s, v, actor="ingest")
            except Exception as exc:
                record_event("metadata", f"Metadata generation failed: {exc}", level="error", video_id=v.id)
        try:
            sched = get_schedule(s)
            slots = scheduling.next_free_slots(s, sched, 1)
            v.suggested_publish_at = slots[0] if slots else None
        except Exception:
            pass
        vid = v.id
    record_event("ingest", f"Imported clip {mp4.name} as video #{vid} (validation {'ok' if vres.ok else 'FAILED'})",
                 level="info" if vres.ok else "warning", video_id=vid)
    return vid
