from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = None
SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None, sqlite_timeout: float = 30):
    """(Re)create the engine. Tests call this with an in-memory/temporary DB."""
    global engine, SessionLocal
    url = url or settings.resolved_database_url
    connect_args = {"check_same_thread": False, "timeout": sqlite_timeout} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def create_all() -> None:
    from . import models  # noqa: F401  (register models)
    from .studio import models as _studio_models  # noqa: F401  (AI Information Video Studio tables)

    Base.metadata.create_all(engine)
    _add_missing_columns()
    # Make sure the single settings/schedule row exists so later reads never need to write.
    from .services.quota import get_schedule

    with session_scope() as s:
        get_schedule(s)


@contextmanager
def session_scope() -> Iterator[Session]:
    assert SessionLocal is not None, "init_engine() not called"
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    assert SessionLocal is not None
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _add_missing_columns() -> None:
    """Tiny migration helper: add columns that exist on models but not yet in the database."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
