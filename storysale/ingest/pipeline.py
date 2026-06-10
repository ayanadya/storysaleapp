"""Orchestrator. Two top-level flows:

  run_posts(...)   → fetch posts → dedupe → OCR? (no — posts use caption only)
                     → extract clothing/brand/price → gate → persist row
  run_stories(...) → fetch stories → OCR each → story_signal.detect →
                     aggregate to per-account verdict → upsert account_signal

run(...) calls both and is what the CLI's `scrape` invokes by default.

Inject Source + OCR callable so this can be unit-tested without IG or easyocr.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from storysale.db import repo
from storysale.ingest import extract
from storysale.ingest import filter as gate_mod
from storysale.ingest import story_signal
from storysale.ingest.fetch import AuthExpired, RateLimited, RawItem, Source, make_thumbnail
from storysale.ingest.ocr import OcrResult

log = logging.getLogger(__name__)

BACKFILL_SECONDS = 14 * 24 * 3600  # 14 days — wide enough to catch weekly-cadence sellers
STORY_LOOKBACK_SECONDS = 24 * 3600  # story-sale verdict only considers last 24h
OcrFn = Callable[[bytes], OcrResult]


# ---------- shared stats container ----------

@dataclass
class RunStats:
    posts_seen: int = 0
    posts_stored: int = 0
    posts_rejected: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    accounts_checked: int = 0
    accounts_with_sale: int = 0
    stories_scanned: int = 0
    status: str = "ok"
    error: Optional[str] = None

    # Compat shim so old call sites (tests/CLI) that referenced .seen/.stored/.rejected
    # keep working when only the post flow runs.
    @property
    def seen(self) -> int: return self.posts_seen + self.stories_scanned
    @property
    def stored(self) -> int: return self.posts_stored
    @property
    def rejected(self) -> int: return self.posts_rejected


# ---------- entrypoints ----------

def run(
    *,
    conn: sqlite3.Connection,
    source: Source,
    ocr: OcrFn,
    accounts: Optional[list[str]] = None,
    thumb_dir: Path = Path("data/thumbs"),
    now: Optional[int] = None,
    backfill_seconds: int = BACKFILL_SECONDS,
    mode: str = "both",   # 'posts' | 'stories' | 'both'
) -> RunStats:
    """Top-level driver. CLI calls this. `mode` lets callers run just one flow."""
    now = now if now is not None else int(time.time())
    accounts = accounts if accounts is not None else repo.list_accounts(conn)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    run_id = repo.start_run(conn)
    stats = RunStats()
    log.info("pipeline: run_id=%d accounts=%d mode=%s backfill=%ds",
             run_id, len(accounts), mode, backfill_seconds)

    try:
        for account in accounts:
            stats.accounts_checked += 1
            log.info("pipeline: --- account %d/%d @%s ---",
                     stats.accounts_checked, len(accounts), account)
            try:
                if mode in ("posts", "both"):
                    _ingest_posts_for(
                        conn=conn, source=source,
                        account=account, since_ts=now - backfill_seconds,
                        thumb_dir=thumb_dir, stats=stats,
                    )
                if mode in ("stories", "both"):
                    had_sale = _ingest_stories_for(
                        conn=conn, source=source, ocr=ocr,
                        account=account, now=now, stats=stats,
                    )
                    if had_sale:
                        stats.accounts_with_sale += 1
                # Per-account success → stamp last_scraped_at so the rotation
                # picks a different account next batch. On failure we leave it
                # alone so the failing account stays near the front of the
                # queue and gets retried promptly.
                repo.mark_account_scraped(conn, account, now=now)
            except (AuthExpired, RateLimited):
                # Whole-session problem — bail out, don't bother with other accounts.
                raise
            except Exception as e:  # noqa: BLE001
                # Per-account failure (404, bad profile, soft-block lying about
                # existence, etc.). Log and move on so one bad handle doesn't
                # tank the rest of the run.
                log.warning("skipping @%s: %s: %s", account, type(e).__name__, e)
                stats.status = "partial"
                continue
    except AuthExpired as e:
        stats.status = "auth_expired"
        stats.error = str(e)
    except RateLimited as e:
        stats.status = "partial"
        stats.error = f"rate limited: {e}"
    except Exception as e:  # noqa: BLE001 — surface anything unexpected via run record
        stats.status = "failed"
        stats.error = f"{type(e).__name__}: {e}"
        log.exception("pipeline run failed")
    finally:
        repo.finish_run(
            conn, run_id,
            seen=stats.posts_seen + stats.stories_scanned,
            stored=stats.posts_stored,
            rejected=stats.posts_rejected,
            status=stats.status, error=stats.error,
        )
        log.info(
            "pipeline: run_id=%d done status=%s posts_seen=%d posts_stored=%d "
            "posts_rejected=%d stories=%d accounts=%d with_sale=%d",
            run_id, stats.status, stats.posts_seen, stats.posts_stored,
            stats.posts_rejected, stats.stories_scanned,
            stats.accounts_checked, stats.accounts_with_sale,
        )
        if stats.rejected_by_reason:
            log.info("pipeline: rejected_by_reason=%s", dict(stats.rejected_by_reason))

    return stats


# Convenience wrappers that match the old `pipeline.run(...)` signature.
def run_posts(**kwargs) -> RunStats: return run(mode="posts", **kwargs)
def run_stories(**kwargs) -> RunStats: return run(mode="stories", **kwargs)


# ---------- post flow ----------

def _ingest_posts_for(
    *,
    conn: sqlite3.Connection,
    source: Source,
    account: str,
    since_ts: int,
    thumb_dir: Path,
    stats: RunStats,
) -> None:
    for item in source.posts(account, since_ts):
        _ingest_post_item(conn=conn, item=item, thumb_dir=thumb_dir, stats=stats)


def _ingest_post_item(
    *,
    conn: sqlite3.Connection,
    item: RawItem,
    thumb_dir: Path,
    stats: RunStats,
) -> None:
    stats.posts_seen += 1

    if item.is_reel:
        log.debug("pipeline.post %s: rejected (reel)", item.ig_id)
        _bump_reject(stats, "reel")
        return
    if repo.exists(conn, item.ig_id):
        log.debug("pipeline.post %s: already in DB, skipping", item.ig_id)
        stats.posts_seen -= 1
        return

    ex = extract.extract(item.caption, "")  # posts: caption only, no OCR
    log.debug(
        "pipeline.post %s: extracted prices=%s clothing=%s brands=%s sold=%s",
        item.ig_id, ex.prices, ex.clothing, ex.brands, ex.sold,
    )
    decision = gate_mod.gate(ex)
    if decision.decision == "reject":
        log.debug("pipeline.post %s: gate REJECT reason=%s", item.ig_id, decision.reason)
        _bump_reject(stats, decision.reason)
        return
    log.debug("pipeline.post %s: gate KEEP", item.ig_id)

    thumb_path: Optional[str] = None
    if item.image_bytes:
        thumb_bytes = make_thumbnail(item.image_bytes)
        thumb_filename = f"{item.ig_id.replace(':', '_')}.jpg"
        full_path = thumb_dir / thumb_filename
        full_path.write_bytes(thumb_bytes)
        thumb_path = (
            str(full_path.relative_to(thumb_dir.parent))
            if thumb_dir.parent in full_path.parents
            else thumb_filename
        )
        log.debug("pipeline.post %s: wrote thumb %s (%dB)", item.ig_id, thumb_path, len(thumb_bytes))

    row = repo.ContentRow(
        ig_id=item.ig_id, account=item.account, kind=item.kind,
        posted_at=item.posted_at, ig_url=item.ig_url,
        caption=item.caption, ocr_text="",
        prices=ex.prices, clothing=ex.clothing, brands=ex.brands,
        thumbnail_path=thumb_path, needs_review=False,
    )
    if repo.insert_content(conn, row):
        log.info("pipeline.post %s: STORED account=@%s prices=%s", item.ig_id, item.account, ex.prices)
        stats.posts_stored += 1
    else:
        log.debug("pipeline.post %s: insert returned False (race/dupe)", item.ig_id)
        stats.posts_seen -= 1


# ---------- story flow (account-level verdict, no per-row storage) ----------

def _ingest_stories_for(
    *,
    conn: sqlite3.Connection,
    source: Source,
    ocr: OcrFn,
    account: str,
    now: int,
    stats: RunStats,
) -> bool:
    """Walk stories for an account, OCR each, classify, upsert verdict.
    Returns True if account is in 'active_sale' state.
    """
    cutoff = now - STORY_LOOKBACK_SECONDS
    counts = {"active_sale": 0, "offers_only": 0, "sold": 0, "none": 0}
    snippets: list[str] = []
    prices_seen: list[int] = []
    needs_review = False
    story_count_24h = 0
    # Track the youngest/oldest posted_at among ACTIVE-SALE stories so the UI
    # can compute "expires in N hours" and hide cards whose stories are gone.
    newest_active_at = 0
    oldest_active_at = 0

    for item in source.stories(account):
        if item.posted_at < cutoff:
            log.debug("pipeline.story %s: older than 24h cutoff, skip", item.ig_id)
            continue
        story_count_24h += 1
        stats.stories_scanned += 1

        ocr_text = ""
        if item.image_bytes:
            ocr_result = ocr(item.image_bytes)
            ocr_text = ocr_result.text
            if ocr_result.needs_review:
                needs_review = True
            log.debug("pipeline.story %s: OCR returned %d chars needs_review=%s",
                      item.ig_id, len(ocr_text), ocr_result.needs_review)
        text = " ".join(t for t in (item.caption, ocr_text) if t)
        sig = story_signal.detect(text)
        cls = story_signal.classify(sig)
        log.debug("pipeline.story %s: text=%r → class=%s prices=%s",
                  item.ig_id, text[:120], cls, sig.prices)
        counts[cls] += 1
        prices_seen.extend(sig.prices)
        if cls == "active_sale":
            if sig.snippet:
                snippets.append(sig.snippet)
            if newest_active_at == 0 or item.posted_at > newest_active_at:
                newest_active_at = item.posted_at
            if oldest_active_at == 0 or item.posted_at < oldest_active_at:
                oldest_active_at = item.posted_at

    # account-level state: priority active > offers > sold > none
    if counts["active_sale"] > 0:
        state = "active_sale"
    elif counts["offers_only"] > 0:
        state = "offers_only"
    elif counts["sold"] > 0:
        state = "sold"
    else:
        state = "none"

    # de-dupe prices keep order, cap at 20
    seen_p: set[int] = set()
    prices = [p for p in prices_seen if not (p in seen_p or seen_p.add(p))][:20]

    log.info(
        "pipeline.stories(@%s): done count_24h=%d state=%s (active=%d offers=%d sold=%d none=%d)",
        account, story_count_24h, state,
        counts["active_sale"], counts["offers_only"], counts["sold"], counts["none"],
    )
    repo.upsert_account_signal(conn, repo.AccountSignalRow(
        account=account, checked_at=now,
        story_count_24h=story_count_24h,
        active_sale_count=counts["active_sale"],
        offers_only_count=counts["offers_only"],
        sold_count=counts["sold"],
        state=state,
        snippets=snippets[:5],   # keep UI compact
        prices=prices,
        needs_review=needs_review,
        newest_story_at=newest_active_at,
        oldest_story_at=oldest_active_at,
    ))
    return state == "active_sale"


def _bump_reject(stats: RunStats, reason: str) -> None:
    stats.posts_rejected += 1
    stats.rejected_by_reason[reason] = stats.rejected_by_reason.get(reason, 0) + 1
