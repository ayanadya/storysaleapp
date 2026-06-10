"""Central logging configuration.

Goal: when something silently produces 0 rows, the operator should be able to
open data/scrape.log and see every step of the data path:

    CLI  →  source.posts/stories/own_followees  →  pipeline._ingest_*
         →  extract.extract  →  filter.gate  →  repo.insert_content

Console handler:  INFO by default, DEBUG when --verbose.
File handler:     always DEBUG, rotates at 2 MB × 5, lives at data/scrape.log.

Idempotent — safe to call configure() multiple times in the same process; the
second call replaces handlers instead of duplicating them.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_PATH = Path("data") / "scrape.log"
_FMT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def configure(*, verbose: bool = False, log_path: Path = LOG_PATH) -> Path:
    """Wire up root + storysale + instaloader loggers. Returns the log file path."""
    global _configured

    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    # Remove any handlers a previous configure() (or basicConfig) installed.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)  # let handlers do the filtering

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # instaloader is noisy at INFO; keep file-level DEBUG but quiet the console.
    logging.getLogger("instaloader").setLevel(logging.INFO)

    _configured = True
    logging.getLogger(__name__).info(
        "logging configured: console=%s file=%s",
        "DEBUG" if verbose else "INFO", log_path,
    )
    return log_path


def tail(n: int = 200, log_path: Path = LOG_PATH) -> str:
    """Return the last `n` lines of the log file. Used by the UI to render a
    recent-activity panel without re-tailing on every interaction."""
    if not log_path.exists():
        return "(no log yet — run the scraper first)"
    # Cheap-and-good-enough: read whole file, slice tail. The file rotates at
    # 2 MB so this is bounded.
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])
