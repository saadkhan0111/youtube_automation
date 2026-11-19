"""Background workers for the clipping engine: one analysis at a time, one render at a time."""
from __future__ import annotations

import logging
import queue
import threading

from sqlalchemy import select

from ..db import session_scope
from ..models import CandidateStatus, ClipCandidate, ClipProject, ProjectStatus
from . import analysis, render

log = logging.getLogger("ytshorts.clipper.worker")

_analysis_q: "queue.Queue[int]" = queue.Queue()
_render_q: "queue.Queue[int]" = queue.Queue()
_download_q: "queue.Queue[int]" = queue.Queue()
_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()

# Tests set this to True to run jobs synchronously.
synchronous = False


def _loop(q: "queue.Queue[int]", fn) -> None:
    while True:
        item = q.get()
        try:
            fn(item)
        except Exception:
            log.exception("clipper job failed")
        finally:
            q.task_done()


def _ensure(name: str, q, fn) -> None:
    with _lock:
        t = _threads.get(name)
        if t is None or not t.is_alive():
            t = threading.Thread(target=_loop, args=(q, fn), name=f"clipper-{name}", daemon=True)
            t.start()
            _threads[name] = t


def enqueue_analysis(project_id: int) -> None:
    if synchronous:
        analysis.run_analysis(project_id)
        return
    _ensure("analysis", _analysis_q, analysis.run_analysis)
    _analysis_q.put(project_id)


def enqueue_download(project_id: int) -> None:
    """YouTube imports run in their own queue so a long download never blocks analyses."""
    from . import youtube_import

    if synchronous:
        youtube_import.run_download(project_id)
        return
    _ensure("download", _download_q, youtube_import.run_download)
    _download_q.put(project_id)


def enqueue_render(candidate_id: int) -> None:
    if synchronous:
        render.render_candidate(candidate_id)
        return
    _ensure("render", _render_q, render.render_candidate)
    _render_q.put(candidate_id)


def recover_on_startup() -> None:
    """Jobs interrupted by a restart: analyses are marked failed (re-run is cheap: chunks are cached),
    renders that were queued or running are queued again."""
    requeue = []
    with session_scope() as s:
        for p in s.scalars(select(ClipProject).where(ClipProject.status.in_(list(ProjectStatus.BUSY)))):
            p.status = ProjectStatus.FAILED
            p.error = "Interrupted by an app restart. Click 'Analyze again' (already transcribed parts are reused)."
        for c in s.scalars(select(ClipCandidate).where(ClipCandidate.status.in_([CandidateStatus.QUEUED, CandidateStatus.RENDERING]))):
            c.status = CandidateStatus.QUEUED
            c.render_progress = 0
            requeue.append(c.id)
    for cid in requeue:
        enqueue_render(cid)
