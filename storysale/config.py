"""Loads .env once and exposes config values. Import early."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

IG_USERNAME: str | None = os.getenv("IG_USERNAME") or None
IG_PASSWORD: str | None = os.getenv("IG_PASSWORD") or None

SESSION_PATH = Path(os.getenv("STORYSALE_SESSION_PATH", "secrets/session-burner"))
DB_PATH = Path(os.getenv("STORYSALE_DB_PATH", "data/storysale.db"))
THUMB_DIR = Path(os.getenv("STORYSALE_THUMB_DIR", "data/thumbs"))
