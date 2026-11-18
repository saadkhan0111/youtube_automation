"""Background stage jobs for the Studio (one job at a time, like the clipper's worker).

A stage handler is registered per stage: handler(project_id, report) where report(fraction, message)
updates the progress shown in the dashboard. A handler raises on failure; the stage is then marked
failed with the error and can be retried on its own. Stages without a handler are reported as
"not built yet" instead of pretending to work.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from sqlalchemy import select

from ..db import session_scope
from ..events import record_event
from . import stages
from .models import StudioProject

log = logging.getLogger("ytshorts.studio.worker")

Handler = Callable[[int, Callable[[float, str | None], None]], None]
HANDLERS: dict[str, Handler] = {}

_q: "queue.Queue[tuple[int, str]]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
synchronous = False   # tests run jobs inline


class NotBuiltYet(Exception):
    pass


def register(stage: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[stage] = fn
        return fn
    return deco


def _report(project_id: int, stage: str) -> Callable[[float, str | None], None]:
    def report(fraction: float, message: str | None = None) -> None:
        with session_scope() as s:
            p = s.get(StudioProject, project_id)
            if p is not None:
                stages.progress(p, stage, fraction, message)
    return report


def _run(project_id: int, stage: str) -> None:
    handler = HANDLERS.get(stage)
    try:
        if handler is None:
            raise NotBuiltYet(f"The {stage} stage is not built yet.")
        handler(project_id, _report(project_id, stage))
        with session_scope() as s:
            p = s.get(StudioProject, project_id)
            if p is not None and stages.status(p, stage) == stages.RUNNING:
                stages.finish(p, stage)
        record_event("studio", f"Project #{project_id}: {stage} ready for review")
    except Exception as exc:
        log.exception("studio stage %s failed for project %s", stage, project_id)
        with session_scope() as s:
            p = s.get(StudioProject, project_id)
            if p is not None:
                stages.fail(p, stage, f"{exc.__class__.__name__}: {exc}" if not isinstance(exc, NotBuiltYet) else str(exc))
        record_event("studio", f"Project #{project_id}: {stage} failed: {exc}", level="error")


def _loop() -> None:
    while True:
        project_id, stage = _q.get()
        try:
            _run(project_id, stage)
        finally:
            _q.task_done()


def enqueue(project_id: int, stage: str) -> None:
    """The caller has already marked the stage running (stages.mark_running) and committed."""
    global _thread
    if synchronous:
        _run(project_id, stage)
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="studio-worker", daemon=True)
            _thread.start()
    _q.put((project_id, stage))


def recover_on_startup() -> None:
    """A stage that was running when the app stopped is marked failed so it can be retried."""
    with session_scope() as s:
        for p in s.scalars(select(StudioProject)):
            for stage, entry in p.stages.items():
                if entry.get("status") == stages.RUNNING:
                    stages.fail(p, stage, "Interrupted by an app restart. Run this stage again.")
