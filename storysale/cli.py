"""Command-line driver. Wraps pipeline.run, retention.sweep, account mgmt,
and one-time interactive login.

Usage:
    python -m storysale.cli login --username burner_acct
    python -m storysale.cli accounts add some_resale_acct
    python -m storysale.cli accounts list
    python -m storysale.cli scrape [--account X] [--dry-run]
    python -m storysale.cli sweep
    python -m storysale.cli search "brandy"
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path
from typing import Optional

from . import config, logging_setup
from .db import repo
from .ingest import pipeline, retention

log = logging.getLogger(__name__)

DEFAULT_DB = config.DB_PATH
DEFAULT_THUMB_DIR = config.THUMB_DIR
DEFAULT_SESSION = str(config.SESSION_PATH)


def _open_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return repo.connect(db_path)


# ---------- commands ----------

def cmd_login(args: argparse.Namespace) -> int:
    from .ingest import auth  # lazy — pulls in instaloader
    username = args.username or config.IG_USERNAME
    if not username:
        print("error: --username required (or set IG_USERNAME in .env)", file=sys.stderr)
        return 2
    password = config.IG_PASSWORD or getpass.getpass(f"Instagram password for {username}: ")
    auth.interactive_login(username, password, path=Path(args.session))
    print(f"Saved session to {args.session}")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    conn = _open_db(Path(args.db))
    try:
        if args.action == "add":
            repo.add_account(conn, args.username)
            print(f"Added {args.username}")
        elif args.action == "list":
            for name in repo.list_accounts(conn):
                print(name)
    finally:
        conn.close()
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    log.info("scrape: start mode=%s account=%s dry=%s sync_follows=%s",
             args.mode, args.account, args.dry_run, not args.no_sync_follows)
    conn = _open_db(Path(args.db))
    try:
        source, ocr = _build_source_and_ocr(args)
        if args.account:
            accounts = [args.account]
            log.info("scrape: scoped to single account @%s", args.account)
        elif args.dry_run:
            accounts = ["demo"]  # matches the fake source's canned data
            log.info("scrape: dry-run, using fake source")
        else:
            # Auto-populate the account table from whoever the burner follows.
            # Best-effort: a failure here (IG soft-blocking the /graphql endpoint
            # that own_profile uses) should NOT prevent the actual post/story
            # scrape from running, since those use different endpoints.
            if not args.no_sync_follows:
                try:
                    added = _sync_followees(conn, source)
                    if added:
                        print(f"synced {added} new followed account(s) into the scrape list")
                except Exception as e:
                    print(f"warn: followee sync skipped ({type(e).__name__}: {e})", file=sys.stderr)
            accounts = None      # → repo.list_accounts(conn)
            total_enabled = len(repo.list_accounts(conn))
            log.info("scrape: %d enabled account(s) total", total_enabled)
            if not total_enabled:
                print(
                    "no accounts in the scrape list yet. Add some manually:\n"
                    "  python -m storysale.cli accounts add <handle>\n"
                    "(the auto-sync from IG follows is unreliable while IG is rate-limiting the burner.)",
                    file=sys.stderr,
                )
                log.warning("scrape: no accounts to scrape, exiting early")
                return 0
            # Pick the least-recently-scraped batch_size accounts. Fairness
            # over time: with batch_size=56 every 30 min, a 2k-account
            # followee list cycles in ~18h (matches IG story TTL).
            accounts = repo.list_accounts_for_scrape(
                conn,
                batch_size=args.batch_size if args.batch_size > 0 else None,
            )
            if not accounts:
                log.info("scrape: rotation picked 0 accounts (all fresh?), nothing to do")
                return 0
            log.info("scrape: this run scraping %d account(s) (oldest-first): %s",
                     len(accounts),
                     accounts if len(accounts) <= 20 else f"{accounts[:20]}…")
        stats = pipeline.run(
            conn=conn, source=source, ocr=ocr,
            accounts=accounts,
            thumb_dir=Path(args.thumb_dir),
            mode=args.mode,
        )
        print(
            f"status={stats.status}  posts_seen={stats.posts_seen}  posts_stored={stats.posts_stored}  "
            f"posts_rejected={stats.posts_rejected}  stories={stats.stories_scanned}  "
            f"accounts={stats.accounts_checked}  with_sale={stats.accounts_with_sale}"
        )
        if stats.rejected_by_reason:
            for reason, count in sorted(stats.rejected_by_reason.items()):
                print(f"  rejected[{reason}] = {count}")
        if stats.error:
            print(f"error: {stats.error}", file=sys.stderr)
        return 0 if stats.status in ("ok", "partial") else 1
    finally:
        conn.close()


def cmd_signals(args: argparse.Namespace) -> int:
    """Print latest story-sale verdicts per account."""
    import json as _json
    conn = _open_db(Path(args.db))
    try:
        state = args.state if args.state != "all" else None
        rows = repo.list_account_signals(conn, state=state)
        if not rows:
            print("(no account_signal rows yet — run `scrape` first)")
            return 0
        for r in rows:
            tag = {
                "active_sale": "🟢 SALE",
                "offers_only": "🟡 offers",
                "sold":        "🔴 sold",
                "none":        "⚪ none",
            }.get(r["state"], r["state"])
            print(f"{tag}  @{r['account']}  stories_24h={r['story_count_24h']}  "
                  f"active={r['active_sale_count']} offers={r['offers_only_count']} sold={r['sold_count']}")
            for s in _json.loads(r["snippets_json"] or "[]")[:2]:
                print(f"    > {s}")
    finally:
        conn.close()
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """One-IG-call session health check. Doesn't touch the DB or the pipeline."""
    from .ingest import auth
    username = args.username or config.IG_USERNAME
    if not username:
        print("error: --username required (or set IG_USERNAME in .env)", file=sys.stderr)
        return 2

    if args.source == "instagrapi":
        client = auth.ensure_instagrapi_session(username, config.IG_PASSWORD)
        print(f"loaded instagrapi session for @{username}")
        alive, detail = auth.check_instagrapi_session_alive(client)
        if alive:
            print(f"[OK] instagrapi session is ALIVE — IG confirms login as @{detail}")
            return 0
        print(f"[FAIL] instagrapi session ping failed: {detail}")
        return 1

    # legacy: instaloader
    session_path = Path(args.session)
    print(f"session file: {session_path} (exists: {session_path.exists()})")
    if not session_path.exists():
        print("→ no session file. Run `python -m storysale.cli login` first.")
        return 1
    loader = auth.load_session(username, path=session_path)
    print(f"loaded session for @{username}")
    alive, detail = auth.check_session_alive(loader)
    if alive:
        print(f"✓ session is ALIVE — IG confirms login as @{detail}")
        print("  empty/null responses are coming from IG-side shadow-block on this IP, not a dead session.")
        return 0
    # `test_login` uses the same deprecated /graphql/query endpoint that's been
    # 401-ing all our other calls. An empty result is therefore ambiguous:
    # either the session is dead, OR the session is fine and IG is just
    # throttling this endpoint. The 'please wait' wording in the 401 (vs
    # 'login required') leans toward the latter.
    print(f"? session status INCONCLUSIVE — test_login() returned: {detail}")
    print()
    print("  If the response above mentions 'please wait' or 401:")
    print("    → session is probably still alive, IG is rate-limiting this endpoint.")
    print("    → Do NOT re-login (re-login during a soft-block makes things worse).")
    print("    → Wait 24h, then re-run this command.")
    print()
    print("  If the response mentions 'login required' or 'session expired':")
    print(f"    → re-login: python -m storysale.cli login --username {username}")
    return 1


def cmd_sweep(args: argparse.Namespace) -> int:
    conn = _open_db(Path(args.db))
    try:
        result = retention.sweep(conn, Path(args.thumb_dir))
        print(f"rows_deleted={result['rows_deleted']}  files_deleted={result['files_deleted']}  files_missing={result['files_missing']}")
    finally:
        conn.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = _open_db(Path(args.db))
    try:
        rows = repo.search(conn, args.query, limit=args.limit)
        if not rows:
            print("(no matches)")
            return 0
        for r in rows:
            print(f"[{r['kind']}] {r['account']}  {r['brand'] or '-'}  prices={r['prices_json']}")
            print(f"  {r['ig_url']}")
            text = (r['caption'] or r['ocr_text'])[:120].replace("\n", " ")
            print(f"  {text}")
    finally:
        conn.close()
    return 0


# ---------- wiring ----------

def _build_source_and_ocr(args: argparse.Namespace):
    """Choose real or fake implementations based on --dry-run."""
    if args.dry_run:
        import time
        from .ingest.fetch import RawItem
        from .ingest.ocr import OcrResult

        def _ocr(_b: bytes) -> OcrResult:
            return OcrResult(text="", needs_review=False, raw_tokens=[])

        class _DrySource:
            """Sample posts (menswear/archive brands) and stories (sale + offer
            mix) so the pipeline runs end-to-end without IG. image_bytes=None
            skips thumbnail writes (no PIL needed)."""
            def posts(self, account, since_ts):
                now = int(time.time())
                yield RawItem(ig_id="post:dry1", account=account, kind="post",
                              posted_at=now, ig_url="https://www.instagram.com/p/dry1/",
                              caption="rick owens hoodie, $250")
                yield RawItem(ig_id="post:dry2", account=account, kind="post",
                              posted_at=now, ig_url="https://www.instagram.com/p/dry2/",
                              caption="margiela leather jacket, asking 400")

            def stories(self, account):
                now = int(time.time())
                # 3 stories: one active sale, one offer-only, one random.
                yield RawItem(ig_id="story:dry1", account=account, kind="story",
                              posted_at=now, ig_url=f"https://www.instagram.com/stories/{account}/dry1/",
                              caption="STORY SALE 🔥 $30 each")
                yield RawItem(ig_id="story:dry2", account=account, kind="story",
                              posted_at=now, ig_url=f"https://www.instagram.com/stories/{account}/dry2/",
                              caption="h/o on this piece")
                yield RawItem(ig_id="story:dry3", account=account, kind="story",
                              posted_at=now, ig_url=f"https://www.instagram.com/stories/{account}/dry3/",
                              caption="just chilling at the park")

        return _DrySource(), _ocr

    from .ingest import auth
    from .ingest.fetch import InstagrapiSource, InstaloaderSource
    from .ingest.ocr import read_image_bytes
    username = args.username or config.IG_USERNAME
    if not username:
        raise SystemExit("error: --username required (or set IG_USERNAME in .env)")

    if args.source == "instagrapi":
        client = auth.ensure_instagrapi_session(username, config.IG_PASSWORD)
        alive, detail = auth.check_instagrapi_session_alive(client)
        if not alive:
            log.warning("scrape: instagrapi session ping failed (%s) — proceeding anyway", detail)
        return InstagrapiSource(client), read_image_bytes

    # legacy: instaloader (kept for comparison / fallback only)
    loader = auth.ensure_session(username, config.IG_PASSWORD, path=Path(args.session))
    alive, detail = auth.check_session_alive(loader)
    if not alive:
        log.warning(
            "scrape: continuing with anonymous instaloader session (test_login: %s). "
            "Most profile lookups will return null. Re-login is probably needed.",
            detail,
        )
    return InstaloaderSource(loader), read_image_bytes


def _sync_followees(conn, source) -> int:
    """Add every account the authenticated burner follows to the local account
    table. Returns the number of newly-added rows. Silently no-ops if the source
    doesn't expose own_followees() (e.g. the dry-run fake source)."""
    if not hasattr(source, "own_followees"):
        log.debug("sync: source has no own_followees(), skipping")
        return 0
    existing = set(repo.list_accounts(conn))
    log.info("sync: starting followee sync (%d already in DB)", len(existing))
    added = 0
    for handle in source.own_followees():
        log.debug("sync: yielded followee @%s", handle)
        if handle not in existing:
            repo.add_account(conn, handle)
            added += 1
            existing.add(handle)
    log.info("sync: done, added=%d total_now=%d", added, len(existing))
    return added


# ---------- entrypoint ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="storysale")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--thumb-dir", default=str(DEFAULT_THUMB_DIR))
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG to console (file log is always DEBUG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="interactive one-time login; pickles session")
    p_login.add_argument("--username", default=None, help="defaults to IG_USERNAME from .env")
    p_login.add_argument("--session", default=DEFAULT_SESSION)
    p_login.set_defaults(func=cmd_login)

    p_acc = sub.add_parser("accounts")
    p_acc_sub = p_acc.add_subparsers(dest="action", required=True)
    p_add = p_acc_sub.add_parser("add"); p_add.add_argument("username")
    p_list = p_acc_sub.add_parser("list")
    p_acc.set_defaults(func=cmd_accounts)

    p_scrape = sub.add_parser("scrape")
    p_scrape.add_argument("--account", help="scrape only this account (otherwise all enabled)")
    p_scrape.add_argument("--username", default=None, help="burner username; defaults to IG_USERNAME from .env")
    p_scrape.add_argument("--session", default=DEFAULT_SESSION)
    p_scrape.add_argument("--dry-run", action="store_true", help="use fake source, no IG calls")
    p_scrape.add_argument("--mode", choices=("posts", "stories", "both"), default="both",
                          help="which flow to run; default both")
    p_scrape.add_argument("--no-sync-follows", action="store_true",
                          help="skip pulling the burner's IG followees into the scrape list")
    p_scrape.add_argument("--source", choices=("instagrapi", "instaloader"), default="instagrapi",
                          help="IG backend; default instagrapi (instaloader is kept for fallback only)")
    # --batch-size is the new name; --max-accounts kept as an alias so existing
    # cron jobs / scripts don't break.
    p_scrape.add_argument("--batch-size", "--max-accounts", type=int, default=0,
                          dest="batch_size",
                          help="how many accounts to scrape this run (0=all enabled). "
                               "With --batch-size N running on a cron, the rotation "
                               "cycles through every account over (total/N) * interval.")
    p_scrape.set_defaults(func=cmd_scrape)

    p_diag = sub.add_parser("diagnose", help="check IG session liveness (one call, no DB writes)")
    p_diag.add_argument("--username", default=None, help="defaults to IG_USERNAME from .env")
    p_diag.add_argument("--session", default=DEFAULT_SESSION)
    p_diag.add_argument("--source", choices=("instagrapi", "instaloader"), default="instagrapi")
    p_diag.set_defaults(func=cmd_diagnose)

    p_sweep = sub.add_parser("sweep"); p_sweep.set_defaults(func=cmd_sweep)

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_sig = sub.add_parser("signals", help="show latest story-sale verdicts per account")
    p_sig.add_argument("--state", choices=("all", "active_sale", "offers_only", "sold", "none"),
                       default="active_sale", help="filter by state; default active_sale")
    p_sig.set_defaults(func=cmd_signals)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    # Windows default console is cp1252 → crashes on emoji output.
    # reconfigure() is a no-op on already-UTF-8 streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    logging_setup.configure(verbose=args.verbose)
    log.info("cli: cmd=%s db=%s thumb_dir=%s", args.cmd, args.db, args.thumb_dir)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
