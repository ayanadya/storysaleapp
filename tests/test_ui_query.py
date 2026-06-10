"""Tests for the UI's query layer. Avoids importing streamlit by importing the
specific functions only when streamlit is installed; otherwise we exercise the
SQL via repo + a re-implementation. We import directly — if streamlit is not
available, the test is skipped.
"""

from __future__ import annotations

import time

import pytest

from storysale.db import repo
from storysale.db.repo import ContentRow

streamlit = pytest.importorskip("streamlit")  # skip module if streamlit not installed
from storysale.ui.app import query_rows, set_feedback  # noqa: E402


@pytest.fixture
def conn():
    c = repo.connect(":memory:")
    # Seed three rows.
    now = int(time.time())
    repo.insert_content(c, ContentRow(
        ig_id="post:a", account="alice", kind="post", posted_at=now,
        ig_url="https://example/p/a", caption="brandy melville skirt $25",
        prices=[25], clothing=["skirt"], brands=["Brandy Melville"],
    ))
    repo.insert_content(c, ContentRow(
        ig_id="post:b", account="bob", kind="post", posted_at=now,
        ig_url="https://example/p/b", caption="nike sneakers $80",
        prices=[80], clothing=["sneakers"], brands=["Nike"],
    ))
    repo.insert_content(c, ContentRow(
        ig_id="story:c", account="alice", kind="story", posted_at=now - 3600,
        ig_url="https://example/s/c", caption="", ocr_text="vintage levi jeans $35",
        prices=[35], clothing=["jeans"], needs_review=True,
    ))
    yield c
    c.close()


def test_fts_search_filters_to_matches(conn):
    rows = query_rows(conn, search_text="brandy")
    assert [r["ig_id"] for r in rows] == ["post:a"]


def test_brand_filter(conn):
    rows = query_rows(conn, brand="Nike")
    assert [r["ig_id"] for r in rows] == ["post:b"]


def test_account_filter(conn):
    rows = query_rows(conn, account="alice")
    assert sorted(r["ig_id"] for r in rows) == ["post:a", "story:c"]


def test_price_range_filter(conn):
    rows = query_rows(conn, min_price=50, max_price=100)
    assert [r["ig_id"] for r in rows] == ["post:b"]


def test_clothing_filter(conn):
    rows = query_rows(conn, clothing="jeans")
    assert [r["ig_id"] for r in rows] == ["story:c"]


def test_only_needs_review(conn):
    rows = query_rows(conn, only_needs_review=True)
    assert [r["ig_id"] for r in rows] == ["story:c"]


def test_empty_search_returns_all_recent(conn):
    rows = query_rows(conn)
    assert len(rows) == 3


def test_set_feedback_persists(conn):
    set_feedback(conn, "post:a", 1)
    row = conn.execute("SELECT feedback FROM content WHERE ig_id='post:a'").fetchone()
    assert row["feedback"] == 1
    set_feedback(conn, "post:a", -1)
    row = conn.execute("SELECT feedback FROM content WHERE ig_id='post:a'").fetchone()
    assert row["feedback"] == -1
