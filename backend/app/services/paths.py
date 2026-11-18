"""Path safety: uploads are restricted to files inside the library directory."""
from __future__ import annotations

from pathlib import Path

from ..config import settings


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_in_library(path: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = settings.library_dir / p
    p = p.resolve()
    if not _inside(p, settings.library_dir):
        raise ValueError("Refusing to use a file outside the clip library directory.")
    return p


def ensure_in_incoming(path: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = settings.incoming_dir / p
    p = p.resolve()
    if not _inside(p, settings.incoming_dir):
        raise ValueError("Path must be inside the incoming directory.")
    return p
