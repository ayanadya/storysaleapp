"""Test doubles for pipeline integration tests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from storysale.ingest.fetch import AuthExpired, RateLimited, RawItem
from storysale.ingest.ocr import OcrResult


@dataclass
class FakeSource:
    """Yields canned RawItems per account. Optionally raises after N items."""
    posts_by_account: dict[str, list[RawItem]] = field(default_factory=dict)
    stories_by_account: dict[str, list[RawItem]] = field(default_factory=dict)
    raise_after: Optional[tuple[int, Exception]] = None  # (count, exc)
    _yielded: int = 0

    def _maybe_raise(self) -> None:
        if self.raise_after and self._yielded >= self.raise_after[0]:
            raise self.raise_after[1]

    def posts(self, account: str, since_ts: int) -> Iterable[RawItem]:
        for item in self.posts_by_account.get(account, []):
            if item.posted_at < since_ts:
                continue
            self._maybe_raise()
            self._yielded += 1
            yield item

    def stories(self, account: str) -> Iterable[RawItem]:
        for item in self.stories_by_account.get(account, []):
            self._maybe_raise()
            self._yielded += 1
            yield item


def fake_ocr(text_for: dict[str, str], needs_review_for: Optional[set[str]] = None) -> Callable[[bytes], OcrResult]:
    """Build an OCR callable keyed on the image_bytes content (passed as bytes)."""
    needs_review_for = needs_review_for or set()

    def _ocr(image_bytes: bytes) -> OcrResult:
        key = image_bytes.decode("utf-8", errors="replace")
        return OcrResult(
            text=text_for.get(key, ""),
            needs_review=key in needs_review_for,
            raw_tokens=[],
        )
    return _ocr


def make_post(ig_id: str, account: str, caption: str, *, age_seconds: int = 0, is_reel: bool = False) -> RawItem:
    return RawItem(
        ig_id=f"post:{ig_id}",
        account=account,
        kind="post",
        posted_at=int(time.time()) - age_seconds,
        ig_url=f"https://www.instagram.com/p/{ig_id}/",
        caption=caption,
        image_bytes=b"img:" + ig_id.encode(),
        is_reel=is_reel,
    )


def make_story(ig_id: str, account: str, *, image_marker: str = "", age_seconds: int = 0) -> RawItem:
    """image_bytes is set to a marker so fake_ocr can look up the text per story."""
    return RawItem(
        ig_id=f"story:{ig_id}",
        account=account,
        kind="story",
        posted_at=int(time.time()) - age_seconds,
        ig_url=f"https://www.instagram.com/stories/{account}/{ig_id}/",
        caption="",
        image_bytes=image_marker.encode("utf-8"),
    )
