"""Workflow state machine: Research -> Script -> Storyboard -> Voiceover -> Assets -> Rendering -> Quality check -> Package -> Complete.

Each stage has a status:
  locked    earlier stages are not approved yet
  ready     can be started
  running   a background job is working on it
  review    finished, waiting for the user's approval (nothing continues automatically)
  approved  user approved; the next stage becomes ready
  failed    the job failed; can be retried (only this stage)
Editing an earlier stage re-opens it for review and locks every later stage, because their results
were built on the old version.
"""
from __future__ import annotations

from ..timeutil import utcnow
from .models import StudioProject
from .settings import STAGES

LOCKED, READY, RUNNING, REVIEW, APPROVED, FAILED = "locked", "ready", "running", "review", "approved", "failed"
WORK_STAGES = STAGES[:-1]          # "complete" is a terminal marker, not a job


class StageError(ValueError):
    pass


def initial_stages() -> dict:
    return {s: {"status": READY if s == STAGES[0] else LOCKED, "progress": 0.0, "message": None, "error": None,
                "updated_at": utcnow().isoformat() + "Z"} for s in STAGES}


def _set(p: StudioProject, stage: str, **fields) -> None:
    st = p.stages
    entry = st.get(stage) or {"status": LOCKED, "progress": 0.0, "message": None, "error": None}
    entry.update(fields)
    entry["updated_at"] = utcnow().isoformat() + "Z"
    st[stage] = entry
    p.stages = st


def status(p: StudioProject, stage: str) -> str:
    return (p.stages.get(stage) or {}).get("status", LOCKED)


def _check_stage(stage: str) -> None:
    if stage not in WORK_STAGES:
        raise StageError(f"Unknown stage '{stage}'.")


def can_start(p: StudioProject, stage: str) -> None:
    _check_stage(stage)
    st = status(p, stage)
    if st == RUNNING:
        raise StageError(f"{stage} is already running.")
    if st == LOCKED:
        raise StageError(f"{stage} is locked: approve the earlier stages first.")
    idx = STAGES.index(stage)
    for prev in STAGES[:idx]:
        if status(p, prev) != APPROVED:
            raise StageError(f"Approve '{prev}' before running '{stage}'.")


def mark_running(p: StudioProject, stage: str, message: str = "Queued") -> None:
    can_start(p, stage)
    # re-running a stage invalidates everything after it
    for later in STAGES[STAGES.index(stage) + 1:]:
        _set(p, later, status=LOCKED, progress=0.0, message=None, error=None)
    _set(p, stage, status=RUNNING, progress=0.0, message=message, error=None)
    p.stage = stage


def progress(p: StudioProject, stage: str, fraction: float, message: str | None = None) -> None:
    fields = {"progress": round(max(0.0, min(1.0, fraction)), 3)}
    if message is not None:
        fields["message"] = message
    _set(p, stage, **fields)


def finish(p: StudioProject, stage: str, message: str = "Ready for review") -> None:
    _set(p, stage, status=REVIEW, progress=1.0, message=message, error=None)


def fail(p: StudioProject, stage: str, error: str) -> None:
    _set(p, stage, status=FAILED, message="Failed", error=error[:2000])


def approve(p: StudioProject, stage: str) -> None:
    _check_stage(stage)
    if status(p, stage) != REVIEW:
        raise StageError(f"Only a stage waiting for review can be approved ({stage} is {status(p, stage)}).")
    _set(p, stage, status=APPROVED, message="Approved")
    nxt = STAGES[STAGES.index(stage) + 1]
    if nxt == "complete":
        _set(p, nxt, status=APPROVED, progress=1.0, message="Video complete")
    else:
        _set(p, nxt, status=READY, progress=0.0, message=None, error=None)
    p.stage = nxt


def reopen(p: StudioProject, stage: str) -> None:
    """The user edited this stage's result: back to review, later stages locked."""
    _check_stage(stage)
    if status(p, stage) in (LOCKED, RUNNING, READY):
        raise StageError(f"{stage} has no result to edit yet.")
    _set(p, stage, status=REVIEW, message="Edited: review and approve again")
    for later in STAGES[STAGES.index(stage) + 1:]:
        _set(p, later, status=LOCKED, progress=0.0, message=None, error=None)
    p.stage = stage
