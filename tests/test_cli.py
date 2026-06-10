"""CLI smoke tests. Exercise the plumbing only — no real IG calls."""

from __future__ import annotations

import pytest

from storysale.cli import main


@pytest.fixture
def db_args(tmp_path):
    db = tmp_path / "test.db"
    thumbs = tmp_path / "thumbs"
    return ["--db", str(db), "--thumb-dir", str(thumbs)]


def test_accounts_add_then_list(db_args, capsys):
    assert main(db_args + ["accounts", "add", "alice"]) == 0
    assert main(db_args + ["accounts", "add", "bob"]) == 0
    capsys.readouterr()
    main(db_args + ["accounts", "list"])
    out = capsys.readouterr().out.split()
    assert out == ["alice", "bob"]


def test_dry_run_scrape_persists_demo_posts_and_signal(db_args, capsys):
    rc = main(db_args + ["scrape", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "posts_stored=2" in out
    assert "stories=3" in out
    # The dry-run story set: 1 sale + 1 offer + 1 random → 1 account with sale.
    assert "with_sale=1" in out

    # Search should find one of the dry-run posts.
    main(db_args + ["search", "rick"])
    out = capsys.readouterr().out
    assert "demo" in out


def test_dry_run_scrape_posts_only_mode(db_args, capsys):
    rc = main(db_args + ["scrape", "--dry-run", "--mode", "posts"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "posts_stored=2" in out
    assert "stories=0" in out


def test_dry_run_scrape_stories_only_mode(db_args, capsys):
    rc = main(db_args + ["scrape", "--dry-run", "--mode", "stories"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "posts_stored=0" in out
    assert "stories=3" in out


def test_signals_subcommand_shows_active_sale(db_args, capsys):
    main(db_args + ["scrape", "--dry-run"])
    capsys.readouterr()
    main(db_args + ["signals", "--state", "active_sale"])
    out = capsys.readouterr().out
    assert "@demo" in out
    assert "SALE" in out
    assert "stories_24h=3" in out


def test_signals_empty_db(db_args, capsys):
    main(db_args + ["signals"])
    out = capsys.readouterr().out
    assert "no account_signal" in out


def test_sweep_empty_db_no_error(db_args, capsys):
    rc = main(db_args + ["sweep"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows_deleted=0" in out


def test_search_with_no_results(db_args, capsys):
    main(db_args + ["search", "nonexistent"])
    out = capsys.readouterr().out
    assert "no matches" in out


def test_dry_run_then_second_dry_run_dedupes_posts(db_args, capsys):
    main(db_args + ["scrape", "--dry-run"])
    capsys.readouterr()
    main(db_args + ["scrape", "--dry-run"])
    out = capsys.readouterr().out
    assert "posts_stored=0" in out
