"""Instagram session lifecycle. Wraps instaloader's session file machinery.

Session file is a pickle of cookies+headers — keep out of git, don't share it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:  # pragma: no cover
    import instaloader  # noqa: F401

log = logging.getLogger(__name__)

DEFAULT_SESSION_PATH = config.SESSION_PATH
INSTAGRAPI_SESSION_PATH = Path("secrets") / "instagrapi-session.json"


# ---------- instagrapi (primary backend) ----------

def ensure_instagrapi_session(
    username: str,
    password: str | None,
    *,
    path: Path = INSTAGRAPI_SESSION_PATH,
):
    """Return a logged-in instagrapi Client.

    Strategy:
      1. If a saved session exists, load it AND call login() to validate
         server-side. If that works we keep going; if it raises, fall through.
      2. Otherwise (or after fallback) do a fresh username/password login.
      3. Save settings to `path` after success so subsequent runs reuse them.

    instagrapi's login() is idempotent when settings are already loaded — it
    won't re-do the full handshake unless cookies are dead. So step (1) is cheap
    on the happy path.
    """
    from instagrapi import Client

    if not username:
        raise ValueError("instagrapi session requires a username")
    cl = Client()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            cl.load_settings(path)
            cl.login(username, password or "")
            log.info("instagrapi: loaded existing session for @%s", username)
            return cl
        except Exception as e:  # noqa: BLE001
            log.warning(
                "instagrapi: saved session unusable (%s: %s) — falling back to fresh login",
                type(e).__name__, e,
            )
            cl = Client()  # reset

    if not password:
        raise FileNotFoundError(
            f"No instagrapi session at {path} and no IG_PASSWORD set. "
            "Add IG_PASSWORD to .env so the session can be created."
        )
    cl.login(username, password)
    cl.dump_settings(path)
    log.info("instagrapi: fresh login OK for @%s, saved session to %s", username, path)
    return cl


def check_instagrapi_session_alive(cl) -> tuple[bool, str]:
    """One-call session ping for instagrapi. Uses account_info() which hits the
    iOS-app private API — different from the broken GraphQL endpoints we had to
    deal with under instaloader. Returns (alive, detail)."""
    try:
        me = cl.account_info()
    except Exception as e:  # noqa: BLE001
        log.warning("instagrapi: account_info() raised %s: %s", type(e).__name__, e)
        return False, f"{type(e).__name__}: {e}"
    log.info("instagrapi session: alive, logged in as @%s (pk=%s)", me.username, me.pk)
    return True, me.username


# ---------- instaloader (legacy / fallback) ----------


def check_session_alive(L) -> tuple[bool, str]:
    """Hit instaloader's tiny `test_login()` endpoint and report status.

    Returns (is_alive, detail). `detail` is the username when alive, or the
    failure reason when not. Cheap (one HTTP call), safe to run at scrape start.

    We catch broadly because IG returns weird things during soft-blocks
    (401, 429, 200+empty, redirects) and the caller wants a yes/no, not an
    exception.
    """
    try:
        who = L.test_login()  # returns username or "" if anonymous
    except Exception as e:  # noqa: BLE001
        log.warning("session: test_login() raised %s: %s", type(e).__name__, e)
        return False, f"{type(e).__name__}: {e}"
    if who:
        log.info("session: alive, logged in as @%s", who)
        return True, who
    # NOTE: empty here is ambiguous. test_login() in instaloader 4.13 hits the
    # deprecated /graphql/query?query_hash=... endpoint which is the same one
    # that's been 401-ing for everyone. So empty can mean either "session dead"
    # or "endpoint blocked, session might be fine." The caller surfaces this
    # ambiguity to the user.
    log.warning("session: test_login() returned empty — could mean dead session OR throttled test endpoint")
    return False, "empty (test_login endpoint is itself blocked; session may still be alive)"


class SessionExpired(RuntimeError):
    """Raised when Instagram rejects the saved session. Caller surfaces to UI;
    we never auto-re-login with the password — that triggers bans."""


def load_session(username: str, path: Path = DEFAULT_SESSION_PATH):
    """Load a pickled session for `username`. Returns an authenticated Instaloader.

    Raises FileNotFoundError if the file is missing (user must do interactive login first).
    """
    import instaloader

    if not path.exists():
        raise FileNotFoundError(
            f"No session file at {path}. Run interactive login once: "
            f"python -m storysale.cli login --username {username}"
        )
    L = instaloader.Instaloader()
    L.load_session_from_file(username, filename=str(path))
    return L


def ensure_session(username: str, password: str | None, path: Path = DEFAULT_SESSION_PATH):
    """Return an authenticated Instaloader, creating the session file if missing.

    Used by `scrape` so the UI button works on a fresh machine: if there's no
    session file yet, we log in with the password from .env. If the password is
    also missing, raise the same FileNotFoundError as load_session.
    """
    if path.exists():
        return load_session(username, path=path)
    if not password:
        raise FileNotFoundError(
            f"No session file at {path} and no IG_PASSWORD set. "
            f"Either run `python -m storysale.cli login --username {username}` "
            f"or add IG_PASSWORD to .env."
        )
    return interactive_login(username, password, path=path)


def save_session(L, path: Path = DEFAULT_SESSION_PATH) -> None:
    """Re-pickle the current session — call after every successful run so
    refreshed cookies are persisted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    L.save_session_to_file(filename=str(path))


def interactive_login(username: str, password: str, path: Path = DEFAULT_SESSION_PATH):
    """One-time login from a Python shell. Saves the session file for later runs."""
    import instaloader

    L = instaloader.Instaloader()
    L.login(username, password)
    save_session(L, path)
    return L
