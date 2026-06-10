"""SQLite repository. Thin wrapper around sqlite3 — no ORM."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Retention windows in seconds.
STORY_THUMB_TTL = 24 * 3600
POST_ROW_TTL = 60 * 24 * 3600   # resale captions stay relevant for ~2 months
STORY_ROW_TTL = 30 * 24 * 3600
STORY_LIFETIME = 24 * 3600      # how long an IG story is live; UI uses this to hide expired signals


@dataclass
class ContentRow:
    ig_id: str
    account: str
    kind: str                       # 'post' | 'story'
    posted_at: int
    ig_url: str
    caption: str = ""
    ocr_text: str = ""
    prices: list[int] = field(default_factory=list)
    clothing: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    thumbnail_path: Optional[str] = None
    needs_review: bool = False


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive schema migrations for DBs that predate columns added
    later. `CREATE TABLE IF NOT EXISTS` doesn't backfill columns on existing
    tables, so we run targeted ALTERs and swallow the duplicate-column error.

    Keep migrations additive only — never change types or drop columns here.
    If a destructive change is ever needed, bump a PRAGMA user_version and
    write a real migration."""
    additions = [
        ("account",        "last_scraped_at",  "INTEGER NOT NULL DEFAULT 0"),
        ("account_signal", "newest_story_at",  "INTEGER NOT NULL DEFAULT 0"),
        ("account_signal", "oldest_story_at",  "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table, column, decl in additions:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            log.info("repo._migrate: added %s.%s", table, column)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

    # Indexes that depend on migrated columns. Safe to CREATE IF NOT EXISTS
    # after the ALTERs have landed.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_last_scraped ON account(enabled, last_scraped_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_signal_newest ON account_signal(newest_story_at)")
    conn.commit()


def exists(conn: sqlite3.Connection, ig_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM content WHERE ig_id = ?", (ig_id,))
    return cur.fetchone() is not None


def insert_content(conn: sqlite3.Connection, row: ContentRow, *, scraped_at: Optional[int] = None) -> bool:
    """Insert a row. Returns True if inserted, False if it already existed."""
    scraped_at = scraped_at if scraped_at is not None else int(time.time())
    brand = row.brands[0] if row.brands else None
    try:
        conn.execute(
            """
            INSERT INTO content (
                ig_id, account, kind, posted_at, scraped_at,
                caption, ocr_text,
                prices_json, clothing_json, brand, brands_json,
                ig_url, thumbnail_path, needs_review
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.ig_id, row.account, row.kind, row.posted_at, scraped_at,
                row.caption, row.ocr_text,
                json.dumps(row.prices), json.dumps(row.clothing),
                brand, json.dumps(row.brands),
                row.ig_url, row.thumbnail_path, 1 if row.needs_review else 0,
            ),
        )
        log.debug("repo.insert_content: inserted %s account=@%s kind=%s", row.ig_id, row.account, row.kind)
        return True
    except sqlite3.IntegrityError as e:
        log.debug("repo.insert_content: IntegrityError for %s (likely dupe): %s", row.ig_id, e)
        return False


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[sqlite3.Row]:
    """FTS5 search across caption + ocr_text. Returns rows ordered by recency."""
    cur = conn.execute(
        """
        SELECT c.*
        FROM content c
        JOIN content_fts f ON f.rowid = c.rowid
        WHERE content_fts MATCH ?
        ORDER BY c.posted_at DESC
        LIMIT ?
        """,
        (query, limit),
    )
    return cur.fetchall()


def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_run (started_at) VALUES (?)",
        (int(time.time()),),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    seen: int,
    stored: int,
    rejected: int,
    status: str,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE scrape_run
           SET finished_at = ?, items_seen = ?, items_stored = ?,
               items_rejected = ?, status = ?, error = ?
         WHERE id = ?
        """,
        (int(time.time()), seen, stored, rejected, status, error, run_id),
    )
    conn.commit()


def sweep_retention(conn: sqlite3.Connection, *, now: Optional[int] = None) -> dict:
    """Delete rows past retention. Returns counts + paths of thumbnails to remove on disk.

    Rules:
      - story or post row deleted when age > 30d AND feedback = 0
      - story thumbnails removed when age > 24h regardless of row (caller deletes files)
    Returns: {"rows_deleted": int, "thumbs_to_delete": [path, ...]}.
    """
    now = now if now is not None else int(time.time())
    story_thumb_cutoff = now - STORY_THUMB_TTL
    row_cutoff = now - POST_ROW_TTL  # post/story rows share 30d ttl

    # Thumbnails to clean: any story older than 24h that still has a thumb,
    # plus any row that is about to be deleted.
    thumbs_to_delete: list[str] = []

    cur = conn.execute(
        """
        SELECT thumbnail_path FROM content
         WHERE kind = 'story' AND posted_at < ? AND thumbnail_path IS NOT NULL
        """,
        (story_thumb_cutoff,),
    )
    thumbs_to_delete.extend(r["thumbnail_path"] for r in cur.fetchall())

    cur = conn.execute(
        """
        SELECT thumbnail_path FROM content
         WHERE posted_at < ? AND feedback = 0 AND thumbnail_path IS NOT NULL
        """,
        (row_cutoff,),
    )
    thumbs_to_delete.extend(r["thumbnail_path"] for r in cur.fetchall())

    # Clear story thumbnail paths older than 24h (row stays, image gone).
    conn.execute(
        """
        UPDATE content
           SET thumbnail_path = NULL
         WHERE kind = 'story' AND posted_at < ? AND thumbnail_path IS NOT NULL
        """,
        (story_thumb_cutoff,),
    )

    cur = conn.execute(
        "DELETE FROM content WHERE posted_at < ? AND feedback = 0",
        (row_cutoff,),
    )
    rows_deleted = cur.rowcount or 0
    conn.commit()

    # De-dupe thumb paths while preserving order.
    seen: set[str] = set()
    unique_thumbs = [p for p in thumbs_to_delete if not (p in seen or seen.add(p))]
    return {"rows_deleted": rows_deleted, "thumbs_to_delete": unique_thumbs}


def list_accounts(conn: sqlite3.Connection) -> list[str]:
    """All enabled accounts, alphabetically. Used by the UI's account dropdown
    and by the CLI `accounts list` — both want a stable enumeration, not a
    scrape order."""
    cur = conn.execute("SELECT username FROM account WHERE enabled = 1 ORDER BY username")
    return [r["username"] for r in cur.fetchall()]


def list_accounts_for_scrape(
    conn: sqlite3.Connection,
    *,
    batch_size: Optional[int] = None,
    stale_after_seconds: Optional[int] = None,
    now: Optional[int] = None,
) -> list[str]:
    """Return enabled accounts in least-recently-scraped order.

    The rotation that makes the 18h-cycle math work: each cron run picks the
    `batch_size` accounts whose `last_scraped_at` is oldest, so over time every
    account gets visited equally often.

    `stale_after_seconds` is an optional floor — if set, only accounts whose
    last_scraped_at is older than that are returned. Useful for idle-aware
    crons that should skip a tick if everyone is already fresh enough.
    """
    now = now if now is not None else int(time.time())
    sql = "SELECT username FROM account WHERE enabled = 1"
    params: list = []
    if stale_after_seconds is not None:
        sql += " AND last_scraped_at < ?"
        params.append(now - stale_after_seconds)
    sql += " ORDER BY last_scraped_at ASC, username ASC"
    if batch_size is not None and batch_size > 0:
        sql += " LIMIT ?"
        params.append(batch_size)
    cur = conn.execute(sql, params)
    return [r["username"] for r in cur.fetchall()]


def mark_account_scraped(conn: sqlite3.Connection, username: str, *, now: Optional[int] = None) -> None:
    """Stamp last_scraped_at on this account so the rotation passes it over
    next run. Called by the pipeline after a per-account scrape *succeeds* —
    on a per-account failure we leave the timestamp alone so the rotation
    retries it next pass."""
    now = now if now is not None else int(time.time())
    conn.execute(
        "UPDATE account SET last_scraped_at = ? WHERE username = ?",
        (now, username),
    )
    conn.commit()


def add_account(conn: sqlite3.Connection, username: str) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO account (username, added_at) VALUES (?, ?)",
        (username, int(time.time())),
    )
    conn.commit()
    log.debug("repo.add_account: %s (rowcount=%d)", username, cur.rowcount)


# ---------- account_signal: story-sale verdict per account ----------

@dataclass
class AccountSignalRow:
    account: str
    checked_at: int
    story_count_24h: int = 0
    active_sale_count: int = 0
    offers_only_count: int = 0
    sold_count: int = 0
    state: str = "none"     # 'active_sale' | 'offers_only' | 'sold' | 'none'
    snippets: list[str] = field(default_factory=list)
    prices: list[int] = field(default_factory=list)
    needs_review: bool = False
    # posted_at of newest/oldest active-sale story observed this check.
    # 0 sentinel = none seen. UI uses newest+STORY_LIFETIME to compute "expires
    # in N hours" and to hide signals whose stories are already gone from IG.
    newest_story_at: int = 0
    oldest_story_at: int = 0


def upsert_account_signal(conn: sqlite3.Connection, row: AccountSignalRow) -> None:
    conn.execute(
        """
        INSERT INTO account_signal (
            account, checked_at, story_count_24h,
            active_sale_count, offers_only_count, sold_count,
            state, snippets_json, prices_json, needs_review,
            newest_story_at, oldest_story_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account) DO UPDATE SET
            checked_at = excluded.checked_at,
            story_count_24h = excluded.story_count_24h,
            active_sale_count = excluded.active_sale_count,
            offers_only_count = excluded.offers_only_count,
            sold_count = excluded.sold_count,
            state = excluded.state,
            snippets_json = excluded.snippets_json,
            prices_json = excluded.prices_json,
            needs_review = excluded.needs_review,
            newest_story_at = excluded.newest_story_at,
            oldest_story_at = excluded.oldest_story_at
        """,
        (
            row.account, row.checked_at, row.story_count_24h,
            row.active_sale_count, row.offers_only_count, row.sold_count,
            row.state, json.dumps(row.snippets), json.dumps(row.prices),
            1 if row.needs_review else 0,
            row.newest_story_at, row.oldest_story_at,
        ),
    )
    conn.commit()


def list_account_signals(
    conn: sqlite3.Connection,
    *,
    state: Optional[str] = None,
    hide_expired: bool = True,
    now: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Return latest verdicts. Order: active_sale first (most sale stories),
    then offers_only, then by recency.

    `hide_expired=True` (the default) drops signals whose newest active-sale
    story is already past its 24h IG-live window. That's almost always what
    the UI wants — a 'story sale right now' tab should not show sales that
    aren't viewable anymore. Pass hide_expired=False from CLI / debug paths
    that want to see everything.
    """
    now = now if now is not None else int(time.time())
    expiry_cutoff = now - STORY_LIFETIME  # newest_story_at < this → expired

    clauses: list[str] = []
    params: list = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if hide_expired:
        # Keep rows with newest_story_at = 0 only when we're not filtering by an
        # active state (those rows have no story to expire). For active_sale /
        # offers_only / sold filters, require fresh stories.
        if state in ("active_sale", "offers_only", "sold"):
            clauses.append("newest_story_at >= ?")
            params.append(expiry_cutoff)
        else:
            clauses.append("(newest_story_at = 0 OR newest_story_at >= ?)")
            params.append(expiry_cutoff)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    if state is not None:
        sql = f"SELECT * FROM account_signal{where} ORDER BY active_sale_count DESC, checked_at DESC"
    else:
        sql = f"""
            SELECT * FROM account_signal{where}
            ORDER BY
              CASE state
                WHEN 'active_sale' THEN 0
                WHEN 'offers_only' THEN 1
                WHEN 'sold' THEN 2
                ELSE 3
              END,
              active_sale_count DESC,
              checked_at DESC
        """
    return list(conn.execute(sql, params).fetchall())
