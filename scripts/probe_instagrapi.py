"""Standalone probe: does instagrapi work for our use case on this account/IP?

Tries, in order:
    1. Fresh login with IG_USERNAME / IG_PASSWORD from .env.
       Saves session to secrets/instagrapi-session.json on success.
       Reuses that session on subsequent runs (so we don't re-login every probe).
    2. Look up @instagram (the canary — definitely exists).
    3. Look up the burner's own profile (sanity check).
    4. Pull the first few followees (the thing instaloader couldn't do).
    5. Pull the most recent post for one of the user's three handles.

Each step prints what it got. If a step fails, the next steps are skipped.

Run:
    .venv/Scripts/python.exe scripts/probe_instagrapi.py
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# Make the storysale package importable so we can pull config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storysale import config  # noqa: E402

SESSION_FILE = Path("secrets") / "instagrapi-session.json"
PROBE_HANDLES = ["instagram", "4ttract1on"]


def _step(label: str) -> None:
    print()
    print("=" * 70)
    print(f"STEP: {label}")
    print("=" * 70)


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    from instagrapi import Client
    from instagrapi.exceptions import LoginRequired

    if not config.IG_USERNAME or not config.IG_PASSWORD:
        _fail("IG_USERNAME / IG_PASSWORD not set in .env")
        return 2
    print(f"probing as @{config.IG_USERNAME}")
    print(f"session file: {SESSION_FILE} (exists: {SESSION_FILE.exists()})")

    cl = Client()

    # --- 1. session / login ---
    _step("session bootstrap")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    logged_in = False
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(config.IG_USERNAME, config.IG_PASSWORD)  # validates session
            _ok(f"loaded existing session for @{config.IG_USERNAME}")
            logged_in = True
        except Exception as e:  # noqa: BLE001
            _fail(f"existing session unusable: {type(e).__name__}: {e}")
    if not logged_in:
        try:
            cl.login(config.IG_USERNAME, config.IG_PASSWORD)
            cl.dump_settings(SESSION_FILE)
            _ok(f"fresh login OK, saved session to {SESSION_FILE}")
        except Exception as e:  # noqa: BLE001
            _fail(f"login failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            return 1

    # --- 2. canary ---
    _step("canary: lookup @instagram")
    try:
        info = cl.user_info_by_username("instagram")
        _ok(f"got @{info.username} — full_name={info.full_name!r} followers={info.follower_count}")
    except Exception as e:  # noqa: BLE001
        _fail(f"canary lookup failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    # --- 3. own profile ---
    _step("own profile lookup")
    try:
        me = cl.account_info()
        _ok(f"I am @{me.username} (pk={me.pk})")
    except Exception as e:  # noqa: BLE001
        _fail(f"own profile lookup failed: {type(e).__name__}: {e}")

    # --- 4. followees ---
    _step("own followees (first 10)")
    try:
        user_id = cl.user_id_from_username(config.IG_USERNAME)
        following = cl.user_following(user_id, amount=10)
        if not following:
            _fail("0 followees returned — burner might genuinely follow nobody yet")
        else:
            for pk, user in following.items():
                print(f"    @{user.username}  ({user.full_name})")
            _ok(f"got {len(following)} followee(s)")
    except Exception as e:  # noqa: BLE001
        _fail(f"followee fetch failed: {type(e).__name__}: {e}")

    # --- 5. recent posts (5 most recent, to verify sort order & freshness) ---
    for handle in PROBE_HANDLES:
        _step(f"recent posts: @{handle} (asking for 5)")
        try:
            uid = cl.user_id_from_username(handle)
            medias = cl.user_medias(uid, amount=5)
            if not medias:
                _fail(f"no posts returned for @{handle}")
                continue
            _ok(f"got {len(medias)} post(s) — listing newest-to-oldest as returned:")
            for i, m in enumerate(medias, 1):
                caption_safe = (m.caption_text or "").encode("ascii", "replace").decode("ascii")
                caption_safe = caption_safe[:80].replace("\n", " ")
                print(f"    [{i}] code={m.code}  taken_at={m.taken_at}  type={m.media_type}")
                print(f"        url=https://www.instagram.com/p/{m.code}/")
                print(f"        caption[:80]={caption_safe!r}")
        except Exception as e:  # noqa: BLE001
            _fail(f"post fetch for @{handle} failed: {type(e).__name__}: {e}")

    print()
    print("PROBE COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
