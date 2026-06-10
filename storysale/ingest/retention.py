"""Periodic cleanup. Call after every successful pipeline run, or on a separate cron."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from ..db import repo

log = logging.getLogger(__name__)


def sweep(conn: sqlite3.Connection, thumb_dir: Path, *, now: Optional[int] = None) -> dict:
    """Delete expired rows and orphaned thumbnail files. Returns counts."""
    result = repo.sweep_retention(conn, now=now)
    files_deleted = 0
    files_missing = 0
    for rel in result["thumbs_to_delete"]:
        # thumb_path was stored relative to thumb_dir.parent. Resolve safely.
        candidate = (thumb_dir.parent / rel) if not Path(rel).is_absolute() else Path(rel)
        if not candidate.exists():
            # Maybe stored as bare filename inside thumb_dir.
            candidate = thumb_dir / Path(rel).name
        try:
            candidate.unlink()
            files_deleted += 1
        except FileNotFoundError:
            files_missing += 1
        except OSError as e:
            log.warning("could not delete %s: %s", candidate, e)
    return {
        "rows_deleted": result["rows_deleted"],
        "files_deleted": files_deleted,
        "files_missing": files_missing,
    }
