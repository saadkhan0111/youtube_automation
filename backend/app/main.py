"""FastAPI application entry point.

Run:  python -m app.main     (or: uvicorn app.main:app --host 127.0.0.1 --port 8000)
"""
from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, jobs
from .api import agent, clipper, routes, studio
from .config import settings
from .security import SecurityMiddleware, ensure_safe_bind


class _RedactFilter(logging.Filter):
    """Belt-and-braces: never let token-like strings reach the log files."""

    KEYS = ("refresh_token", "access_token", "client_secret", "ya29.", "1//")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(k in msg for k in self.KEYS):
            record.msg = "[redacted log message that looked like it contained a credential]"
            record.args = ()
        return True


def setup_logging() -> None:
    settings.ensure_dirs()
    root = logging.getLogger()
    if any(getattr(h, "_ytshorts", False) for h in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(settings.data_dir / "logs" / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        h.addFilter(_RedactFilter())
        h._ytshorts = True
        root.addHandler(h)
    root.setLevel(logging.INFO)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    for noisy in ("httpx", "httpx2", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    if db.engine is None:
        db.init_engine()
    db.create_all()
    jobs.start()
    yield
    jobs.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="YouTube Shorts Publisher", lifespan=lifespan)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    )
    app.include_router(routes.router)
    app.include_router(agent.router)
    app.include_router(clipper.router)
    app.include_router(studio.router)

    # Serve the built dashboard (frontend/dist) if present.
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(dist / "index.html")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    ensure_safe_bind()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
