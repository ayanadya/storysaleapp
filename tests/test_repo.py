"""Repo + FTS5 + retention tests against an in-memory SQLite."""

from __future__ import annotations

import time

import pytest

from storysale.db import repo
from storysale.db.repo import AccountSignalRow, ContentRow


@pytest.fixture
def conn():
    c = repo.connect(":memory:")
    yield c
    c.close()


def _row(ig_id="post:abc", **overrides):
    base = dict(
        ig_id=ig_id,
        account="someone",
        kind="post",
        posted_at=int(time.time()),
        ig_url=f"https://www.instagram.com/p/{ig_id.split(':')[1]}/",
        caption="brandy melville skirt $25",
        ocr_text="",
        prices=[25],
        clothing=["skirt"],
        brands=["Brandy Melville"],
        thumbnail_path=f"thumbs/{ig_id.replace(':', '_')}.jpg",
        needs_review=False,
    )
    base.update(overrides)
    return ContentRow(**base)


# ---------- D1, D2: insert + dedupe ----------

def test_D1_insert_new_row(conn):
    assert repo.insert_content(conn, _row()) is True
    rows = conn.execute("SELECT * FROM content").fetchall()
    assert len(rows) == 1
    assert rows[0]["brand"] == "Brandy Melville"


def test_D2_dedupe_returns_false(conn):
    repo.insert_content(conn, _row())
    assert repo.insert_content(conn, _row()) is False
    assert conn.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 1


def test_exists_helper(conn):
    assert repo.exists(conn, "post:abc") is False
    repo.insert_content(conn, _row())
    assert repo.exists(conn, "post:abc") is True


# ---------- D3: FTS5 search ----------

def test_D3_fts_finds_caption_terms(conn):
    repo.insert_content(conn, _row(ig_id="post:a", caption="brandy melville skirt $25"))
    repo.insert_content(conn, _row(ig_id="post:b", caption="nike sneakers $40", clothing=["sneakers"], brands=["Nike"], prices=[40]))
    results = repo.search(conn, "brandy")
    ig_ids = [r["ig_id"] for r in results]
    assert "post:a" in ig_ids
    assert "post:b" not in ig_ids


def test_fts_finds_ocr_text(conn):
    repo.insert_content(conn, _row(ig_id="story:1", kind="story", caption="", ocr_text="vintage levi jeans $30"))
    results = repo.search(conn, "vintage")
    assert len(results) == 1
    assert results[0]["ig_id"] == "story:1"


def test_fts_updates_after_delete(conn):
    repo.insert_content(conn, _row())
    conn.execute("DELETE FROM content WHERE ig_id = 'post:abc'")
    conn.commit()
    assert repo.search(conn, "brandy") == []


# ---------- D4–D6: retention sweep ----------

def test_D4_old_story_thumb_cleared_row_kept_within_30d(conn):
    """Story image expires at 24h. The row still survives (within 30d, no feedback)."""
    now = 1_700_000_000
    old_story_ts = now - 25 * 3600  # 25h ago
    repo.insert_content(conn, _row(ig_id="story:old", kind="story", posted_at=old_story_ts))
    result = repo.sweep_retention(conn, now=now)
    assert "thumbs/story_old.jpg" in result["thumbs_to_delete"]
    # Row still present, thumbnail_path nulled.
    row = conn.execute("SELECT * FROM content WHERE ig_id='story:old'").fetchone()
    assert row is not None
    assert row["thumbnail_path"] is None


def test_D5_story_with_feedback_survives_after_ttl(conn):
    # POST_ROW_TTL is 60 days now; use 61d to be past the retention cutoff so
    # the feedback-keeps-it-alive logic is the only thing the row depends on.
    now = 1_700_000_000
    repo.insert_content(conn, _row(ig_id="story:fb", kind="story", posted_at=now - 61 * 24 * 3600))
    conn.execute("UPDATE content SET feedback = 1 WHERE ig_id = 'story:fb'")
    conn.commit()
    repo.sweep_retention(conn, now=now)
    assert repo.exists(conn, "story:fb")


def test_D6_old_post_no_feedback_deleted(conn):
    # 61 days = past POST_ROW_TTL (60 days), no feedback → should be deleted.
    now = 1_700_000_000
    repo.insert_content(conn, _row(ig_id="post:old", kind="post", posted_at=now - 61 * 24 * 3600))
    result = repo.sweep_retention(conn, now=now)
    assert result["rows_deleted"] == 1
    assert not repo.exists(conn, "post:old")
    assert "thumbs/post_old.jpg" in result["thumbs_to_delete"]


def test_recent_rows_untouched(conn):
    now = 1_700_000_000
    repo.insert_content(conn, _row(ig_id="post:new", kind="post", posted_at=now - 3600))
    result = repo.sweep_retention(conn, now=now)
    assert result["rows_deleted"] == 0
    assert result["thumbs_to_delete"] == []
    assert repo.exists(conn, "post:new")


# ---------- Account list ----------

def test_account_list_empty_by_default(conn):
    assert repo.list_accounts(conn) == []


def test_account_add_idempotent(conn):
    repo.add_account(conn, "alice")
    repo.add_account(conn, "alice")
    repo.add_account(conn, "bob")
    assert repo.list_accounts(conn) == ["alice", "bob"]


# ---------- account_signal: story-sale verdicts ----------

def _sig(account="alice", **overrides):
    base = dict(
        account=account, checked_at=1_700_000_000,
        story_count_24h=3, active_sale_count=1,
        offers_only_count=1, sold_count=0,
        state="active_sale", snippets=["story sale $30"], prices=[30],
        needs_review=False,
    )
    base.update(overrides)
    return AccountSignalRow(**base)


def test_account_signal_insert_and_read(conn):
    repo.upsert_account_signal(conn, _sig())
    rows = repo.list_account_signals(conn)
    assert len(rows) == 1
    assert rows[0]["account"] == "alice"
    assert rows[0]["state"] == "active_sale"
    assert rows[0]["story_count_24h"] == 3


def test_account_signal_upsert_overwrites(conn):
    repo.upsert_account_signal(conn, _sig(active_sale_count=5))
    repo.upsert_account_signal(conn, _sig(active_sale_count=1, state="offers_only"))
    rows = repo.list_account_signals(conn)
    assert len(rows) == 1
    assert rows[0]["state"] == "offers_only"
    assert rows[0]["active_sale_count"] == 1


def test_account_signal_filter_by_state(conn):
    repo.upsert_account_signal(conn, _sig(account="alice", state="active_sale"))
    repo.upsert_account_signal(conn, _sig(account="bob",   state="offers_only", active_sale_count=0, offers_only_count=2))
    repo.upsert_account_signal(conn, _sig(account="carol", state="none", active_sale_count=0, offers_only_count=0))

    # hide_expired=False because _sig() builds rows with newest_story_at=0
    # (the default); this test only exercises the state-filter, not expiration.
    active = repo.list_account_signals(conn, state="active_sale", hide_expired=False)
    assert [r["account"] for r in active] == ["alice"]

    offers = repo.list_account_signals(conn, state="offers_only", hide_expired=False)
    assert [r["account"] for r in offers] == ["bob"]


def test_account_signal_default_ordering_puts_active_first(conn):
    repo.upsert_account_signal(conn, _sig(account="carol", state="none", active_sale_count=0))
    repo.upsert_account_signal(conn, _sig(account="bob",   state="offers_only", active_sale_count=0))
    repo.upsert_account_signal(conn, _sig(account="alice", state="active_sale", active_sale_count=3))

    rows = repo.list_account_signals(conn, hide_expired=False)
    assert [r["account"] for r in rows] == ["alice", "bob", "carol"]


def test_account_signal_hide_expired_filters_stale_stories(conn):
    """A signal whose newest active-sale story posted_at is >24h ago should
    not appear in the default (hide_expired=True) listing."""
    now = 1_700_000_000
    day = 24 * 3600
    # Fresh: newest story 1h ago → still live on IG for ~23h
    repo.upsert_account_signal(conn, _sig(
        account="fresh", state="active_sale",
        newest_story_at=now - 3600, oldest_story_at=now - 3600,
    ))
    # Stale: newest story 30h ago → already off IG
    repo.upsert_account_signal(conn, _sig(
        account="stale", state="active_sale",
        newest_story_at=now - 30 * 3600, oldest_story_at=now - 30 * 3600,
    ))

    visible = repo.list_account_signals(conn, state="active_sale", now=now)
    assert [r["account"] for r in visible] == ["fresh"]

    # show_expired=False → include stale row too
    all_rows = repo.list_account_signals(conn, state="active_sale", hide_expired=False, now=now)
    assert sorted(r["account"] for r in all_rows) == ["fresh", "stale"]


def test_list_accounts_for_scrape_least_recent_first(conn):
    """Rotation: least-recently-scraped accounts come first."""
    now = 1_700_000_000
    repo.add_account(conn, "alice")
    repo.add_account(conn, "bob")
    repo.add_account(conn, "carol")
    # alice scraped now, bob scraped 1h ago, carol never (last_scraped_at=0)
    repo.mark_account_scraped(conn, "alice", now=now)
    repo.mark_account_scraped(conn, "bob", now=now - 3600)

    order = repo.list_accounts_for_scrape(conn, now=now)
    assert order == ["carol", "bob", "alice"]  # 0, then oldest, then newest

    # batch_size caps result
    assert repo.list_accounts_for_scrape(conn, batch_size=2, now=now) == ["carol", "bob"]

    # stale_after_seconds=1800 → only carol (0) and bob (1h old) are stale
    stale = repo.list_accounts_for_scrape(conn, stale_after_seconds=1800, now=now)
    assert stale == ["carol", "bob"]


def test_account_signal_rejects_invalid_state(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_account_signal(conn, _sig(state="bogus"))
