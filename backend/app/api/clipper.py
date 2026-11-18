"""Clipping-engine API: long-video projects, AI moments, rendering."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import record_event
from ..models import CandidateStatus, ClipCandidate, ClipProject, PermissionStatus, ProjectStatus
from ..serializers import iso
from ..clipper import settings as cs
from ..clipper import worker

router = APIRouter(prefix="/api/clipper")
DBDep = Depends(get_db, scope="function")


def candidate_dict(c: ClipCandidate) -> dict:
    return {
        "id": c.id, "project_id": c.project_id, "start_time": c.start_time, "end_time": c.end_time,
        "duration": round(c.end_time - c.start_time, 2), "score": c.score, "category": c.category,
        "headline": c.headline, "hook_line": c.hook_line, "outro_line": c.outro_line, "reason": c.reason,
        "excerpt": c.excerpt, "status": c.status, "render_progress": c.render_progress, "error": c.error,
        "video_id": c.video_id, "updated_at": iso(c.updated_at),
    }


def project_dict(p: ClipProject, with_candidates: bool = False) -> dict:
    d = {
        "id": p.id, "name": p.name, "source_file": Path(p.source_path).name, "source_mode": p.source_mode,
        "source_url": p.source_url, "source_creator": p.source_creator, "permission_status": p.permission_status,
        "permission_notes": p.permission_notes, "topic_hint": p.topic_hint, "duration": p.duration,
        "resolution": f"{p.width}x{p.height}" if p.width else None, "status": p.status, "progress": p.progress,
        "stage_message": p.stage_message, "error": p.error, "max_candidates": p.max_candidates,
        "min_clip_seconds": p.min_clip_seconds, "max_clip_seconds": p.max_clip_seconds, "voice": p.voice,
        "est_cost_usd": p.est_cost_usd, "source_url_preview": f"/api/clipper/projects/{p.id}/source",
        "created_at": iso(p.created_at), "candidate_count": len(p.candidates),
        "rendered_count": sum(1 for c in p.candidates if c.status == CandidateStatus.RENDERED),
    }
    if with_candidates:
        d["candidates"] = [candidate_dict(c) for c in sorted(p.candidates, key=lambda c: c.start_time)
                           if c.status != CandidateStatus.DISCARDED]
    return d


def _project(db: Session, pid: int) -> ClipProject:
    p = db.get(ClipProject, pid)
    if p is None:
        raise HTTPException(404, "Project not found")
    return p


def _candidate(db: Session, cid: int) -> ClipCandidate:
    c = db.get(ClipCandidate, cid)
    if c is None:
        raise HTTPException(404, "Moment not found")
    return c


@router.get("/config")
def config():
    return {
        "openai_key_present": cs.openai_available(),
        "model": cs.OPENAI_MODEL, "transcribe_model": cs.OPENAI_TRANSCRIBE_MODEL, "tts_model": cs.OPENAI_TTS_MODEL,
        "default_voice": cs.OPENAI_TTS_VOICE, "voices": cs.TTS_VOICES, "avatar_present": cs.AVATAR_PATH.is_file(),
        "avatar_url": "/api/clipper/avatar", "whisper_cost_per_min": cs.WHISPER_COST_PER_MIN,
        "sources_dir": str(cs.sources_dir()),
    }


@router.get("/avatar")
def avatar_image():
    if not cs.AVATAR_PATH.is_file():
        raise HTTPException(404, "No avatar image")
    return FileResponse(cs.AVATAR_PATH, media_type="image/png")


@router.get("/voices/{voice_name}/preview")
def voice_preview(voice_name: str):
    """Sample of a voice (OpenAI TTS + the same processing as the Shorts). Cached after the first request."""
    from ..clipper import voice

    if voice_name not in cs.TTS_VOICES:
        raise HTTPException(404, "Unknown voice")
    if not cs.openai_available():
        raise HTTPException(400, "OPENAI_API_KEY is missing in backend/.env")
    try:
        return FileResponse(voice.preview(voice_name), media_type="audio/wav")
    except Exception as exc:
        raise HTTPException(502, f"Voice preview failed: {exc}") from exc


class ProjectPatch(BaseModel):
    voice: str | None = None


@router.patch("/projects/{pid}")
def patch_project(pid: int, body: ProjectPatch, db: Session = DBDep):
    p = _project(db, pid)
    if body.voice is not None:
        if body.voice not in cs.TTS_VOICES:
            raise HTTPException(400, "Unknown voice.")
        p.voice = body.voice
    return project_dict(p)


@router.get("/projects")
def list_projects(db: Session = DBDep):
    return [project_dict(p) for p in db.scalars(select(ClipProject).order_by(ClipProject.id.desc()))]


class ProjectOptions(BaseModel):
    name: str | None = Field(None, max_length=255)
    topic_hint: str | None = Field(None, max_length=255)
    source_url: str | None = None
    source_creator: str | None = None
    permission_status: str = "unknown"
    permission_notes: str | None = None
    max_candidates: int = Field(8, ge=1, le=30)
    min_clip_seconds: int = Field(20, ge=8, le=120)
    max_clip_seconds: int = Field(60, ge=10, le=180)
    voice: str | None = None


class LinkedProject(ProjectOptions):
    source_path: str


def _create(db: Session, path: Path, mode: str, opts: ProjectOptions) -> ClipProject:
    if opts.permission_status not in PermissionStatus.ALL:
        raise HTTPException(400, "Invalid permission status.")
    if opts.min_clip_seconds >= opts.max_clip_seconds:
        raise HTTPException(400, "Minimum length must be below maximum length.")
    if opts.voice and opts.voice not in cs.TTS_VOICES:
        raise HTTPException(400, "Unknown voice.")
    if not cs.openai_available():
        raise HTTPException(400, "OPENAI_API_KEY is missing in backend/.env (needed for transcription and moment detection).")
    p = ClipProject(
        name=(opts.name or path.stem)[:255], source_path=str(path), source_mode=mode, topic_hint=opts.topic_hint,
        source_url=opts.source_url, source_creator=opts.source_creator, permission_status=opts.permission_status,
        permission_notes=opts.permission_notes, max_candidates=opts.max_candidates,
        min_clip_seconds=opts.min_clip_seconds, max_clip_seconds=opts.max_clip_seconds, voice=opts.voice,
        status=ProjectStatus.EXTRACTING, progress=0.0, stage_message="Queued",
    )
    db.add(p)
    db.flush()
    record_event("clipper", f"New clip project #{p.id}: {p.name}")
    return p


@router.post("/projects")
def create_linked(body: LinkedProject, db: Session = DBDep):
    path = Path(body.source_path.strip().strip('"'))
    if not path.is_absolute() or not path.is_file():
        raise HTTPException(400, "File not found. Paste the full path, e.g. D:\\Streams\\my_stream.mp4")
    if path.suffix.lower() not in cs.SOURCE_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {path.suffix}. Use one of: {', '.join(sorted(cs.SOURCE_EXTENSIONS))}")
    p = _create(db, path.resolve(), "linked", body)
    db.commit()
    worker.enqueue_analysis(p.id)
    return project_dict(p)


@router.post("/projects/upload")
def create_uploaded(
    file: UploadFile = File(...), name: str = Form(""), topic_hint: str = Form(""), source_url: str = Form(""),
    source_creator: str = Form(""), permission_status: str = Form("unknown"), permission_notes: str = Form(""),
    max_candidates: int = Form(8), min_clip_seconds: int = Form(20), max_clip_seconds: int = Form(60), voice: str = Form(""),
    db: Session = DBDep,
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in cs.SOURCE_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}.")
    opts = ProjectOptions(name=name or Path(file.filename).stem, topic_hint=topic_hint or None, source_url=source_url or None,
                          source_creator=source_creator or None, permission_status=permission_status,
                          permission_notes=permission_notes or None, max_candidates=max_candidates,
                          min_clip_seconds=min_clip_seconds, max_clip_seconds=max_clip_seconds, voice=voice or None)
    dest = cs.sources_dir() / f"{uuid.uuid4().hex[:8]}{ext}"
    tmp = dest.with_suffix(".uploading")
    with tmp.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=4 * 1024 * 1024)
    tmp.rename(dest)
    try:
        p = _create(db, dest, "uploaded", opts)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    db.commit()
    worker.enqueue_analysis(p.id)
    return project_dict(p)


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = DBDep):
    return project_dict(_project(db, pid), with_candidates=True)


@router.get("/projects/{pid}/source")
def project_source(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    path = Path(p.source_path)
    if not path.is_file():
        raise HTTPException(404, "Source file missing")
    media = "video/mp4" if path.suffix.lower() in (".mp4", ".m4v", ".mov") else "video/" + path.suffix.lstrip(".").lower()
    return FileResponse(path, media_type=media)


@router.post("/projects/{pid}/analyze")
def reanalyze(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    if p.status in ProjectStatus.BUSY:
        raise HTTPException(400, "Analysis is already running.")
    p.status, p.progress, p.error, p.stage_message = ProjectStatus.EXTRACTING, 0.0, None, "Queued"
    db.commit()
    worker.enqueue_analysis(p.id)
    return project_dict(p)


@router.delete("/projects/{pid}")
def delete_project(pid: int, db: Session = DBDep):
    p = _project(db, pid)
    if p.status in ProjectStatus.BUSY or any(c.status in (CandidateStatus.QUEUED, CandidateStatus.RENDERING) for c in p.candidates):
        raise HTTPException(400, "Wait until analysis/rendering has finished.")
    if p.source_mode == "uploaded":
        Path(p.source_path).unlink(missing_ok=True)  # only files we stored ourselves; linked files are never touched
    shutil.rmtree(cs.projects_dir() / str(p.id), ignore_errors=True)
    db.delete(p)
    record_event("clipper", f"Deleted clip project #{pid} (rendered Shorts stay in the Videos library)")
    return {"ok": True}


class CandidatePatch(BaseModel):
    start_time: float | None = Field(None, ge=0)
    end_time: float | None = Field(None, ge=0)
    headline: str | None = Field(None, max_length=120)
    hook_line: str | None = Field(None, max_length=300)
    outro_line: str | None = Field(None, max_length=300)


@router.patch("/candidates/{cid}")
def patch_candidate(cid: int, body: CandidatePatch, db: Session = DBDep):
    c = _candidate(db, cid)
    if c.status in (CandidateStatus.QUEUED, CandidateStatus.RENDERING):
        raise HTTPException(400, "Can't edit while rendering.")
    data = body.model_dump(exclude_unset=True)
    st, en = data.get("start_time", c.start_time), data.get("end_time", c.end_time)
    dur = c.project.duration or en
    if en <= st or en - st < 5 or en - st > 180 or en > dur + 0.5:
        raise HTTPException(400, "Moment must be 5-180 seconds long and inside the video.")
    for k, v in data.items():
        setattr(c, k, v.strip() if isinstance(v, str) else v)
    if c.status in (CandidateStatus.RENDERED, CandidateStatus.FAILED):
        c.status = CandidateStatus.PROPOSED  # edited after rendering: can be rendered again as a new Short
    return candidate_dict(c)


class NewCandidate(BaseModel):
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    headline: str = ""
    hook_line: str = ""
    outro_line: str = ""


@router.post("/projects/{pid}/candidates")
def add_candidate(pid: int, body: NewCandidate, db: Session = DBDep):
    p = _project(db, pid)
    if body.end_time - body.start_time < 5 or body.end_time - body.start_time > 180:
        raise HTTPException(400, "Moment must be 5-180 seconds long.")
    from ..clipper.analysis import load_transcript

    t = load_transcript(p.id) or {"segments": []}
    excerpt = " ".join(s["text"] for s in t["segments"] if s["end"] > body.start_time and s["start"] < body.end_time)[:1500]
    c = ClipCandidate(project_id=p.id, start_time=body.start_time, end_time=body.end_time, score=0, category="manual",
                      headline=body.headline, hook_line=body.hook_line, outro_line=body.outro_line,
                      reason="Added manually", excerpt=excerpt)
    db.add(c)
    db.flush()
    return candidate_dict(c)


class RenderBody(BaseModel):
    candidate_ids: list[int]


@router.post("/projects/{pid}/render")
def render_selected(pid: int, body: RenderBody, db: Session = DBDep):
    p = _project(db, pid)
    ids = []
    for cid in body.candidate_ids:
        c = db.get(ClipCandidate, cid)
        if c is None or c.project_id != p.id:
            raise HTTPException(400, f"Moment {cid} doesn't belong to this project.")
        if c.status in (CandidateStatus.QUEUED, CandidateStatus.RENDERING):
            continue
        c.status, c.render_progress, c.error = CandidateStatus.QUEUED, 0.0, None
        ids.append(c.id)
    db.commit()
    for cid in ids:
        worker.enqueue_render(cid)
    return {"queued": ids}


@router.post("/candidates/{cid}/discard")
def discard(cid: int, db: Session = DBDep):
    c = _candidate(db, cid)
    if c.status in (CandidateStatus.QUEUED, CandidateStatus.RENDERING):
        raise HTTPException(400, "Can't discard while rendering.")
    c.status = CandidateStatus.DISCARDED
    return {"ok": True}
