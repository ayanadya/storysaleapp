"""Integration tests for the pipeline orchestrator with fake source + fake OCR.

Two flows are tested separately:
  POST flow  → caption-only extract, gate, persist content rows
  STORY flow → OCR each, story_signal.detect, upsert account_signal verdict
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from storysale.db import repo
from storysale.ingest import pipeline
from tests.fakes import FakeSource, fake_ocr, make_post, make_story


@pytest.fixture
def conn():
    c = repo.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _stub_thumbnail():
    """Skip PIL — return bytes unchanged so we don't decode fake markers as images."""
    with patch("storysale.ingest.pipeline.make_thumbnail", side_effect=lambda b, target_kb=50: b):
        yield


# ---------- POST flow ----------

def test_I1_post_stored_reel_skipped(conn, tmp_path):
    source = FakeSource(
        posts_by_account={
            "alice": [
                make_post("p1", "alice", "rick owens hoodie $250"),
                make_post("r1", "alice", "new reel drop", is_reel=True),
            ]
        },
    )
    stats = pipeline.run(
        conn=conn, source=source, ocr=fake_ocr({}),
        accounts=["alice"], thumb_dir=tmp_path, mode="posts",
    )
    assert stats.posts_stored == 1
    assert stats.posts_rejected == 1
    assert stats.rejected_by_reason.get("reel") == 1
    rows = conn.execute("SELECT ig_id, brand FROM content").fetchall()
    assert [r["ig_id"] for r in rows] == ["post:p1"]
    assert rows[0]["brand"] == "Rick Owens"


def test_post_rejection_reasons_logged(conn, tmp_path):
    source = FakeSource(
        posts_by_account={
            "alice": [
                make_post("ok", "alice", "rick owens hoodie $250"),
                make_post("no_clothing", "alice", "DROP TONIGHT 8PM"),
                make_post("sold", "alice", "hoodie $200 SOLD"),
            ]
        },
    )
    stats = pipeline.run(
        conn=conn, source=source, ocr=fake_ocr({}),
        accounts=["alice"], thumb_dir=tmp_path, mode="posts",
    )
    assert stats.posts_stored == 1
    assert stats.posts_rejected == 2
    assert stats.rejected_by_reason == {"no_clothing": 1, "sold": 1}


def test_I3_second_post_run_dedupes(conn, tmp_path):
    source = FakeSource(posts_by_account={"alice": [make_post("p1", "alice", "hoodie $250")]})
    s1 = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                      accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    s2 = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                      accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert s1.posts_stored == 1
    assert s2.posts_stored == 0


def test_I4_rate_limit_mid_run_partial_status(conn, tmp_path):
    from storysale.ingest.fetch import RateLimited
    source = FakeSource(
        posts_by_account={
            "alice": [
                make_post("p1", "alice", "rick hoodie $250"),
                make_post("p2", "alice", "margiela jacket $400"),
            ]
        },
        raise_after=(1, RateLimited("429")),
    )
    stats = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                        accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert stats.status == "partial"
    assert stats.posts_stored == 1


def test_I5_auth_expired_aborts(conn, tmp_path):
    from storysale.ingest.fetch import AuthExpired
    source = FakeSource(
        posts_by_account={"alice": [make_post("p1", "alice", "hoodie $250")]},
        raise_after=(0, AuthExpired("session dead")),
    )
    stats = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                        accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert stats.status == "auth_expired"
    assert stats.posts_stored == 0


def test_I8_reel_skipped_before_extract(conn, tmp_path):
    source = FakeSource(
        posts_by_account={
            "alice": [make_post("r1", "alice", "rick hoodie $250 in this video!", is_reel=True)]
        },
    )
    stats = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                        accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert stats.posts_stored == 0
    assert stats.rejected_by_reason.get("reel") == 1


def test_post_flow_does_not_use_ocr(conn, tmp_path):
    """Caption-only for posts — OCR callable must not be invoked."""
    calls = []
    def tracking_ocr(image_bytes):
        calls.append(image_bytes)
        from storysale.ingest.ocr import OcrResult
        return OcrResult(text="", needs_review=False, raw_tokens=[])

    source = FakeSource(posts_by_account={"alice": [make_post("p1", "alice", "hoodie $250")]})
    pipeline.run(conn=conn, source=source, ocr=tracking_ocr,
                 accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert calls == []


def test_backfill_window_drops_old_posts(conn, tmp_path):
    source = FakeSource(
        posts_by_account={
            "alice": [
                make_post("recent", "alice", "hoodie $250", age_seconds=3600),
                # BACKFILL_SECONDS is 14 days; 30d old must fall outside any
                # reasonable widening of that window.
                make_post("ancient", "alice", "hoodie $250", age_seconds=30 * 24 * 3600),
            ]
        },
    )
    stats = pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                        accounts=["alice"], thumb_dir=tmp_path, mode="posts")
    assert stats.posts_stored == 1
    assert repo.exists(conn, "post:recent")
    assert not repo.exists(conn, "post:ancient")


# ---------- STORY flow ----------

def test_story_active_sale_classified_and_upserted(conn, tmp_path):
    source = FakeSource(
        stories_by_account={
            "alice": [make_story("s1", "alice", image_marker="sale1")]
        },
    )
    ocr = fake_ocr({"sale1": "STORY SALE $30 each"})

    stats = pipeline.run(conn=conn, source=source, ocr=ocr,
                        accounts=["alice"], thumb_dir=tmp_path, mode="stories")

    assert stats.stories_scanned == 1
    assert stats.accounts_with_sale == 1
    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig["state"] == "active_sale"
    assert sig["story_count_24h"] == 1
    assert sig["active_sale_count"] == 1
    assert "$30" in (json.loads(sig["snippets_json"]) or [""])[0]


def test_story_offer_only_disqualifies_account(conn, tmp_path):
    source = FakeSource(
        stories_by_account={"alice": [make_story("s1", "alice", image_marker="offer")]},
    )
    ocr = fake_ocr({"offer": "h/o on this piece"})
    stats = pipeline.run(conn=conn, source=source, ocr=ocr,
                        accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    assert stats.accounts_with_sale == 0
    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig["state"] == "offers_only"
    assert sig["offers_only_count"] == 1


def test_story_aggregates_mixed_per_account(conn, tmp_path):
    """3 stories: 1 active sale, 1 offer-only, 1 random. Account is active_sale
    because >=1 sale story; counts reflect all three."""
    source = FakeSource(
        stories_by_account={
            "alice": [
                make_story("s1", "alice", image_marker="sale"),
                make_story("s2", "alice", image_marker="offer"),
                make_story("s3", "alice", image_marker="random"),
            ]
        },
    )
    ocr = fake_ocr({
        "sale": "selling this hoodie $250",
        "offer": "h/o starting at $200",
        "random": "just chilling",
    })
    pipeline.run(conn=conn, source=source, ocr=ocr,
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig["state"] == "active_sale"
    assert sig["story_count_24h"] == 3
    assert sig["active_sale_count"] == 1
    assert sig["offers_only_count"] == 1
    # 1 "random" story doesn't match anything → 'none' bucket, not stored separately


def test_story_old_stories_excluded_from_24h_window(conn, tmp_path):
    source = FakeSource(
        stories_by_account={
            "alice": [
                make_story("s_recent", "alice", image_marker="x", age_seconds=3600),
                make_story("s_old",    "alice", image_marker="x", age_seconds=30 * 3600),
            ]
        },
    )
    ocr = fake_ocr({"x": "$50"})
    pipeline.run(conn=conn, source=source, ocr=ocr,
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig["story_count_24h"] == 1   # old one excluded


def test_story_account_with_no_stories_still_upserts(conn, tmp_path):
    source = FakeSource(stories_by_account={"alice": []})
    pipeline.run(conn=conn, source=source, ocr=fake_ocr({}),
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig is not None
    assert sig["story_count_24h"] == 0
    assert sig["state"] == "none"


def test_story_second_run_overwrites_signal(conn, tmp_path):
    """Stories are ephemeral; account_signal is the latest verdict."""
    source1 = FakeSource(stories_by_account={"alice": [make_story("s1", "alice", image_marker="sale")]})
    pipeline.run(conn=conn, source=source1, ocr=fake_ocr({"sale": "story sale $30"}),
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")

    source2 = FakeSource(stories_by_account={"alice": [make_story("s2", "alice", image_marker="off")]})
    pipeline.run(conn=conn, source=source2, ocr=fake_ocr({"off": "h/o $300"}),
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")

    sig = conn.execute("SELECT * FROM account_signal WHERE account='alice'").fetchone()
    assert sig["state"] == "offers_only"   # latest verdict, sale state gone


def test_story_does_not_persist_content_rows(conn, tmp_path):
    """Stories must NOT end up in the content table — that's posts-only."""
    source = FakeSource(stories_by_account={"alice": [make_story("s1", "alice", image_marker="x")]})
    ocr = fake_ocr({"x": "story sale $30"})
    pipeline.run(conn=conn, source=source, ocr=ocr,
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    assert conn.execute("SELECT COUNT(*) FROM content").fetchone()[0] == 0


# ---------- BOTH mode ----------

def test_run_both_handles_posts_and_stories_in_one_pass(conn, tmp_path):
    source = FakeSource(
        posts_by_account={"alice": [make_post("p1", "alice", "rick hoodie $250")]},
        stories_by_account={"alice": [make_story("s1", "alice", image_marker="sale")]},
    )
    ocr = fake_ocr({"sale": "story sale $30"})
    stats = pipeline.run(conn=conn, source=source, ocr=ocr,
                        accounts=["alice"], thumb_dir=tmp_path, mode="both")
    assert stats.posts_stored == 1
    assert stats.stories_scanned == 1
    assert stats.accounts_with_sale == 1


def test_I7_empty_account_list_logs_clean_run(conn, tmp_path):
    stats = pipeline.run(conn=conn, source=FakeSource(), ocr=fake_ocr({}),
                        accounts=[], thumb_dir=tmp_path)
    assert stats.posts_stored == 0
    assert stats.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM scrape_run").fetchone()[0] == 1


def test_ocr_low_confidence_propagates_to_account_signal(conn, tmp_path):
    source = FakeSource(stories_by_account={"alice": [make_story("s1", "alice", image_marker="blurry")]})
    ocr = fake_ocr({"blurry": "story sale $30"}, needs_review_for={"blurry"})
    pipeline.run(conn=conn, source=source, ocr=ocr,
                 accounts=["alice"], thumb_dir=tmp_path, mode="stories")
    sig = conn.execute("SELECT needs_review FROM account_signal WHERE account='alice'").fetchone()
    assert sig["needs_review"] == 1
