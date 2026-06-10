"""instaloader fetch layer. Defined behind a `Source` protocol so the pipeline
can be tested with a fake implementation that doesn't touch the network.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass
class RawItem:
    ig_id: str
    account: str
    kind: str               # 'post' or 'story'
    posted_at: int          # unix seconds
    ig_url: str
    caption: str = ""
    image_bytes: Optional[bytes] = None
    is_reel: bool = False


@runtime_checkable
class Source(Protocol):
    """Anything that can yield RawItems for an account. Pipeline depends on this,
    not on instaloader directly."""

    def posts(self, account: str, since_ts: int) -> Iterable[RawItem]: ...
    def stories(self, account: str) -> Iterable[RawItem]: ...


class AuthExpired(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


def _classify_ig_error(exc: Exception) -> Exception:
    """Map an instaloader ConnectionException to AuthExpired vs RateLimited.

    IG returns 401 in two very different situations:
      • Real auth failure → message mentions 'login_required' or similar.
      • Soft rate-limit / shadow-block → message is 'Please wait a few minutes…'
        with a 401 status. Classifying that as AuthExpired is misleading and
        causes us to wipe the session.
    """
    msg = str(exc).lower()
    if "please wait" in msg or "try again" in msg or "429" in msg or "rate" in msg:
        return RateLimited(str(exc))
    if "login_required" in msg or "login required" in msg or "checkpoint" in msg:
        return AuthExpired(str(exc))
    # Unclassified 401 — lean RateLimited rather than burn the session.
    if "401" in msg:
        return RateLimited(str(exc))
    return exc


class InstaloaderSource:
    """Real implementation. Wraps an authenticated Instaloader instance.

    Rate limiting: jittered sleep between items so we look human-ish. Tune the
    floor up if we still get 429s in practice.
    """

    SLEEP_MIN = 4.0
    SLEEP_MAX = 8.0

    def __init__(self, loader, *, sleep_min: float = SLEEP_MIN, sleep_max: float = SLEEP_MAX):
        self._L = loader
        self._sleep_min = sleep_min
        self._sleep_max = sleep_max

    def _sleep(self) -> None:
        time.sleep(random.uniform(self._sleep_min, self._sleep_max))

    def _profile(self, account: str):
        import instaloader
        try:
            return instaloader.Profile.from_username(self._L.context, account)
        except instaloader.exceptions.LoginRequiredException as e:
            raise AuthExpired(str(e)) from e
        except instaloader.exceptions.ConnectionException as e:
            raise _classify_ig_error(e) from e

    def posts(self, account: str, since_ts: int) -> Iterable[RawItem]:
        import instaloader
        log.info("fetch.posts(@%s): begin (since_ts=%d)", account, since_ts)
        profile = self._profile(account)
        log.debug("fetch.posts(@%s): got profile (userid=%s)", account, profile.userid)
        yielded = 0
        for post in profile.get_posts():
            if int(post.date_utc.timestamp()) < since_ts:
                log.debug("fetch.posts(@%s): hit since_ts cutoff at shortcode=%s", account, post.shortcode)
                break  # ordered newest-first
            is_reel = getattr(post, "is_video", False) and getattr(post, "typename", "") == "GraphVideo"
            try:
                image_bytes = self._fetch_image(post.url) if not is_reel else None
            except instaloader.exceptions.ConnectionException as e:
                if "429" in str(e):
                    raise RateLimited(str(e)) from e
                raise
            yielded += 1
            log.debug(
                "fetch.posts(@%s): yield #%d shortcode=%s is_reel=%s image=%dB caption=%r",
                account, yielded, post.shortcode, is_reel,
                len(image_bytes) if image_bytes else 0,
                (post.caption or "")[:80],
            )
            yield RawItem(
                ig_id=f"post:{post.shortcode}",
                account=account,
                kind="post",
                posted_at=int(post.date_utc.timestamp()),
                ig_url=f"https://www.instagram.com/p/{post.shortcode}/",
                caption=post.caption or "",
                image_bytes=image_bytes,
                is_reel=is_reel,
            )
            self._sleep()
        log.info("fetch.posts(@%s): done, yielded %d posts", account, yielded)

    def own_followees(self) -> Iterable[str]:
        """Yield usernames the authenticated burner follows. Uses Profile.own_profile
        (instead of from_username) because IG often returns 'profile does not exist'
        when an account tries to look itself up via the public lookup endpoint."""
        import instaloader
        log.info("fetch.own_followees: begin")
        count = 0
        try:
            profile = instaloader.Profile.own_profile(self._L.context)
            log.debug("fetch.own_followees: got burner profile (userid=%s)", profile.userid)
            for followee in profile.get_followees():
                count += 1
                log.debug("fetch.own_followees: yielded @%s (#%d)", followee.username, count)
                yield followee.username
                self._sleep()
        except instaloader.exceptions.LoginRequiredException as e:
            log.warning("fetch.own_followees: login required → AuthExpired")
            raise AuthExpired(str(e)) from e
        except instaloader.exceptions.ConnectionException as e:
            log.warning("fetch.own_followees: connection error (yielded %d before fail): %s", count, e)
            raise _classify_ig_error(e) from e
        log.info("fetch.own_followees: done, yielded %d followees", count)

    def stories(self, account: str) -> Iterable[RawItem]:
        import instaloader
        log.info("fetch.stories(@%s): begin", account)
        profile = self._profile(account)
        log.debug("fetch.stories(@%s): got profile (userid=%s)", account, profile.userid)
        try:
            stories_iter = self._L.get_stories(userids=[profile.userid])
        except instaloader.exceptions.LoginRequiredException as e:
            log.warning("fetch.stories(@%s): login required → AuthExpired", account)
            raise AuthExpired(str(e)) from e
        yielded = 0
        for story in stories_iter:
            for item in story.get_items():
                try:
                    image_bytes = self._fetch_image(item.url)
                except instaloader.exceptions.ConnectionException as e:
                    if "429" in str(e):
                        raise RateLimited(str(e)) from e
                    raise
                yielded += 1
                log.debug(
                    "fetch.stories(@%s): yield #%d mediaid=%s image=%dB",
                    account, yielded, item.mediaid,
                    len(image_bytes) if image_bytes else 0,
                )
                yield RawItem(
                    ig_id=f"story:{item.mediaid}",
                    account=account,
                    kind="story",
                    posted_at=int(item.date_utc.timestamp()),
                    ig_url=f"https://www.instagram.com/stories/{account}/{item.mediaid}/",
                    caption="",
                    image_bytes=image_bytes,
                )
                self._sleep()
        log.info("fetch.stories(@%s): done, yielded %d stories", account, yielded)

    @staticmethod
    def _fetch_image(url: str) -> bytes:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()


# ---------------------------------------------------------------------------
# instagrapi backend (primary, used by default since 2026-06)
# ---------------------------------------------------------------------------

def _classify_instagrapi_error(exc: Exception) -> Exception:
    """Map instagrapi exceptions onto our AuthExpired / RateLimited / unchanged."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("LoginRequired", "ChallengeRequired", "ReloginAttemptExceeded"):
        return AuthExpired(f"{name}: {exc}")
    if name in ("PleaseWaitFewMinutes", "RateLimitError", "ProxyAddressIsBlocked"):
        return RateLimited(f"{name}: {exc}")
    if "please wait" in msg or "rate limit" in msg:
        return RateLimited(f"{name}: {exc}")
    return exc


class InstagrapiSource:
    """Source backed by instagrapi's iOS-app private API.

    Replaces InstaloaderSource as the primary backend — instaloader's GraphQL
    query hashes were retired by Meta in 2026 and stopped returning real data.
    instagrapi uses the iOS app endpoints which are actively maintained.

    Implements the same `Source` protocol so the pipeline doesn't care which
    backend is wired in.
    """

    SLEEP_MIN = 3.0
    SLEEP_MAX = 7.0

    def __init__(self, client, *, sleep_min: float = SLEEP_MIN, sleep_max: float = SLEEP_MAX):
        self._cl = client
        self._sleep_min = sleep_min
        self._sleep_max = sleep_max
        # Cache username→user_id lookups so a multi-stage scrape (posts then
        # stories for the same account) doesn't pay the lookup twice.
        self._uid_cache: dict[str, int] = {}

    def _sleep(self) -> None:
        time.sleep(random.uniform(self._sleep_min, self._sleep_max))

    def _uid(self, account: str) -> int:
        if account in self._uid_cache:
            return self._uid_cache[account]
        try:
            uid = int(self._cl.user_id_from_username(account))
        except Exception as e:  # noqa: BLE001
            raise _classify_instagrapi_error(e) from e
        self._uid_cache[account] = uid
        return uid

    def _pinned_pks(self, uid: int) -> set[int]:
        """Return the set of pinned-post pks for a user. Pinned posts break the
        newest-first ordering instagrapi returns from user_medias, so the post
        iterator skips them — they're almost always 'feedback' or 'shop info'
        anchors, not actual sale listings."""
        try:
            info = self._cl.user_info(uid)
        except Exception as e:  # noqa: BLE001
            # Pinned lookup is best-effort. If it fails, return empty set and
            # let the caller proceed — worst case is the pinned post leaks
            # through and gets rejected by the gate as not-a-listing.
            log.warning("instagrapi: user_info(%s) failed (%s), pinned-skip disabled", uid, e)
            return set()
        pks = getattr(info, "pinned_media_pks", None) or []
        result = {int(p) for p in pks}
        if result:
            log.debug("instagrapi: user %s has %d pinned post(s): %s", uid, len(result), result)
        return result

    def own_followees(self) -> Iterable[str]:
        """Yield usernames the authenticated burner follows."""
        log.info("fetch.own_followees: begin (instagrapi)")
        count = 0
        try:
            my_uid = int(self._cl.user_id)
            # amount=0 = all followees. This can be hundreds; pace per yield.
            followees = self._cl.user_following(my_uid, amount=0)
        except Exception as e:  # noqa: BLE001
            log.warning("fetch.own_followees: failed: %s: %s", type(e).__name__, e)
            raise _classify_instagrapi_error(e) from e
        for pk, user in followees.items():
            count += 1
            log.debug("fetch.own_followees: yielded @%s (#%d, pk=%s)", user.username, count, pk)
            yield user.username
        log.info("fetch.own_followees: done, yielded %d followees", count)

    def posts(self, account: str, since_ts: int) -> Iterable[RawItem]:
        log.info("fetch.posts(@%s): begin (instagrapi, since_ts=%d)", account, since_ts)
        uid = self._uid(account)
        log.debug("fetch.posts(@%s): uid=%s", account, uid)

        pinned = self._pinned_pks(uid)

        # Pull a window of recent posts. 50 covers a 14-day backfill for all
        # but the most prolific accounts; on each subsequent scrape repo.exists()
        # dedupes so the marginal cost of a wide window is small.
        try:
            medias = self._cl.user_medias(uid, amount=50)
        except Exception as e:  # noqa: BLE001
            log.warning("fetch.posts(@%s): user_medias failed: %s", account, e)
            raise _classify_instagrapi_error(e) from e

        yielded = 0
        for m in medias:
            if int(m.pk) in pinned:
                log.debug("fetch.posts(@%s): skip pk=%s (pinned)", account, m.pk)
                continue
            posted_at = int(m.taken_at.timestamp())
            if posted_at < since_ts:
                log.debug("fetch.posts(@%s): hit since_ts cutoff at code=%s (posted_at=%d)",
                          account, m.code, posted_at)
                # Don't break — instagrapi can return pinned posts mid-list, so
                # an old post here doesn't mean every subsequent one is older.
                # Just skip and continue.
                continue

            # media_type: 1=photo, 2=video, 8=carousel. product_type "clips" = reel.
            is_reel = (m.media_type == 2 and getattr(m, "product_type", "") == "clips")

            image_bytes: Optional[bytes] = None
            if not is_reel:
                try:
                    thumb_url = str(m.thumbnail_url) if m.thumbnail_url else None
                    if thumb_url:
                        image_bytes = self._fetch_image(thumb_url)
                except Exception as e:  # noqa: BLE001
                    # Image fetch failures shouldn't kill the whole post —
                    # we can still store the row with caption/metadata.
                    log.warning("fetch.posts(@%s): image fetch failed for %s: %s",
                                account, m.code, e)

            yielded += 1
            caption = m.caption_text or ""
            log.debug(
                "fetch.posts(@%s): yield #%d code=%s is_reel=%s image=%dB caption=%r",
                account, yielded, m.code, is_reel,
                len(image_bytes) if image_bytes else 0,
                caption[:80],
            )
            yield RawItem(
                ig_id=f"post:{m.code}",
                account=account,
                kind="post",
                posted_at=posted_at,
                ig_url=f"https://www.instagram.com/p/{m.code}/",
                caption=caption,
                image_bytes=image_bytes,
                is_reel=is_reel,
            )
            self._sleep()
        log.info("fetch.posts(@%s): done, yielded %d posts (skipped %d pinned)",
                 account, yielded, len(pinned))

    def stories(self, account: str) -> Iterable[RawItem]:
        log.info("fetch.stories(@%s): begin (instagrapi)", account)
        uid = self._uid(account)
        try:
            items = self._cl.user_stories(uid)
        except Exception as e:  # noqa: BLE001
            log.warning("fetch.stories(@%s): user_stories failed: %s", account, e)
            raise _classify_instagrapi_error(e) from e

        yielded = 0
        for s in items:
            try:
                thumb_url = str(s.thumbnail_url) if s.thumbnail_url else None
                image_bytes = self._fetch_image(thumb_url) if thumb_url else None
            except Exception as e:  # noqa: BLE001
                log.warning("fetch.stories(@%s): image fetch failed for %s: %s",
                            account, s.pk, e)
                image_bytes = None

            yielded += 1
            log.debug(
                "fetch.stories(@%s): yield #%d pk=%s image=%dB",
                account, yielded, s.pk,
                len(image_bytes) if image_bytes else 0,
            )
            yield RawItem(
                ig_id=f"story:{s.pk}",
                account=account,
                kind="story",
                posted_at=int(s.taken_at.timestamp()),
                ig_url=f"https://www.instagram.com/stories/{account}/{s.pk}/",
                caption="",  # stories don't have captions in instagrapi's model
                image_bytes=image_bytes,
            )
            self._sleep()
        log.info("fetch.stories(@%s): done, yielded %d stories", account, yielded)

    @staticmethod
    def _fetch_image(url: str) -> bytes:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()


def make_thumbnail(image_bytes: bytes, target_kb: int = 50) -> bytes:
    """Resize + JPEG-recompress to ~target_kb. Used by pipeline before persisting."""
    import io
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # Start at 600px wide, drop quality until under target.
    img.thumbnail((600, 600))
    for quality in (80, 70, 60, 50, 40, 30):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= target_kb * 1024:
            return buf.getvalue()
    return buf.getvalue()  # best effort
