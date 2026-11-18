"""Audit/event logging to both the Python log and the event_log table.

Events are written by a background writer thread so that recording an event
from inside an open database transaction never blocks on SQLite's write lock.
"""
from __future__ import annotations

import logging
import queue
import threading

from .models import EventLog
from .timeutil import utcnow

logger = logging.getLogger("ytshorts")

_queue: "queue.Queue[dict | None]" = queue.Queue()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()


def _write_loop() -> None:
    from .db import session_scope

    while True:
        item = _queue.get()
        try:
            if item is None:
                return
            batch = [item]
            while True:
                try:
                    nxt = _queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    _queue.task_done()
                    break
                batch.append(nxt)
            try:
                with session_scope() as s:
                    s.add_all(EventLog(**e) for e in batch)
            except Exception:
                logger.exception("failed to persist %d event(s)", len(batch))
            finally:
                for _ in batch[1:]:
                    _queue.task_done()
        finally:
            _queue.task_done()


def _ensure_writer() -> None:
    global _writer
    with _writer_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_write_loop, name="event-writer", daemon=True)
            _writer.start()


def record_event(
    category: str,
    message: str,
    level: str = "info",
    video_id: int | None = None,
    upload_id: int | None = None,
    actor: str | None = None,
) -> None:
    log_fn = getattr(logger, level if level in {"debug", "info", "warning", "error"} else "info")
    log_fn("[%s] %s (video=%s upload=%s actor=%s)", category, message, video_id, upload_id, actor)
    _ensure_writer()
    _queue.put({"timestamp": utcnow(), "category": category, "message": message[:4000], "level": level,
                "video_id": video_id, "upload_id": upload_id, "actor": actor})


def flush_events(timeout: float = 10.0) -> None:
    """Wait until queued events are written (used in tests and on shutdown)."""
    import time

    deadline = time.time() + timeout
    while _queue.unfinished_tasks and time.time() < deadline:
        time.sleep(0.02)
