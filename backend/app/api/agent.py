"""Agent integration API (/api/agent/*), for an orchestrator such as Claude Code,
OpenClaw or a cron script. Requires `Authorization: Bearer <AGENT_API_TOKEN>`.

Agents can read state, generate metadata, flag clips for human review and queue
uploads of clips a HUMAN has already approved. They cannot approve, clear
rights, delete, change settings or bypass quota limits; those checks live in
services/workflow.py and apply to every caller.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..events import record_event
from ..models import Upload, Video, VideoStatus
from ..serializers import upload_dict, video_dict
from ..services import metadata, quota, scheduling, workflow
from ..services.quota import get_schedule
from ..timeutil import parse_local

router = APIRouter(prefix="/api/agent")
# Commit before the response is sent (so the next request sees the change).
DBDep = Depends(get_db, scope="function")
ACTOR = "agent"


def _video(db: Session, vid: int) -> Video:
    v = db.get(Video, vid)
    if v is None:
        raise HTTPException(404, "Video not found")
    return v


@router.get("/pending-clips")
def pending_clips(db: Session = DBDep):
    tz = get_schedule(db).timezone
    return [video_dict(v, tz, include_uploads=False) for v in db.scalars(select(Video).where(Video.status == VideoStatus.PENDING_REVIEW))]


@router.get("/approved-clips")
def approved_clips(db: Session = DBDep):
    tz = get_schedule(db).timezone
    return [video_dict(v, tz, include_uploads=False) for v in db.scalars(select(Video).where(Video.status == VideoStatus.APPROVED))]


@router.get("/videos/{video_id}")
def video(video_id: int, db: Session = DBDep):
    return video_dict(_video(db, video_id), get_schedule(db).timezone)


@router.get("/uploads/{upload_id}")
def upload_status(upload_id: int, db: Session = DBDep):
    up = db.get(Upload, upload_id)
    if up is None:
        raise HTTPException(404, "Upload not found")
    return upload_dict(up, get_schedule(db).timezone)


@router.get("/quota")
def quota_status(db: Session = DBDep):
    return quota.status(db)


@router.post("/videos/{video_id}/generate-metadata")
def gen_metadata(video_id: int, db: Session = DBDep):
    v = _video(db, video_id)
    if v.status != VideoStatus.PENDING_REVIEW:
        raise HTTPException(400, "Agents may only generate metadata for clips pending review.")
    return metadata.generate_metadata(db, v, actor=ACTOR)


class ReviewRequest(BaseModel):
    message: str | None = None


@router.post("/videos/{video_id}/request-approval")
def request_approval(video_id: int, body: ReviewRequest, db: Session = DBDep):
    v = _video(db, video_id)
    if v.status != VideoStatus.PENDING_REVIEW:
        raise HTTPException(400, f"Video is '{v.status}', not pending review.")
    record_event("review", f"Agent requested human review: {body.message or ''}", video_id=v.id, actor=ACTOR)
    return {"ok": True, "status": v.status, "note": "A human must approve this clip in the dashboard."}


class AgentUpload(BaseModel):
    mode: Literal["private", "unlisted", "scheduled"]
    publish_at: str | None = None


@router.post("/videos/{video_id}/upload")
def queue_upload(video_id: int, body: AgentUpload, db: Session = DBDep):
    from ..jobs import trigger_queue

    v = _video(db, video_id)
    when = None
    if body.publish_at:
        try:
            when = parse_local(body.publish_at, get_schedule(db).timezone)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        up = workflow.enqueue_upload(db, v, body.mode, when, ACTOR)
    except (workflow.WorkflowError, scheduling.ScheduleError) as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    trigger_queue()
    return upload_dict(up, get_schedule(db).timezone)
