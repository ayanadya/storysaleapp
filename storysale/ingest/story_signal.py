"""Per-story signal detector for the story-sale notifier.

Stories are NOT searchable rows. They're inputs to an account-level verdict:
'is this account having a sale right now?'

Detection rules (per story):
  positive = sale_language OR has_price
  disqualifier = h/o style phrases OR the literal 'sold'
  classification (disqualifier wins):
    if disqualifier_sold:            'sold'
    elif disqualifier_offer:         'offers_only'
    elif positive:                   'active_sale'
    else:                            'none'

Aggregation (per account, over the last 24h of stories) is done in pipeline.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ..dicts import offer_disqualifiers, sale_markers

Classification = Literal["active_sale", "offers_only", "sold", "none"]

_SOLD_WORD = re.compile(r"(?<![a-z0-9])sold(?![a-z0-9])", re.I)
_DOLLAR = re.compile(r"\$\s*\d{1,4}")
_EURO = re.compile(r"€\s*\d{1,4}|\d{1,4}\s*€")
_DIGITS_2_4 = re.compile(r"(?<!\d)\d{2,4}(?!\d)")


def _phrase_pattern(s: str) -> re.Pattern[str]:
    """Word-ish boundary that copes with '/' in 'h/o', 'c/o'."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", re.I)


@dataclass
class StorySignal:
    has_sale_language: bool = False
    has_price: bool = False
    has_offer_only: bool = False
    has_sold: bool = False
    prices: list[int] = field(default_factory=list)
    snippet: str = ""


def detect(text: str) -> StorySignal:
    """Run the detectors over text (already-joined caption + OCR). Pure."""
    if not text:
        return StorySignal()

    text_l = text.lower()
    sig = StorySignal()

    # --- sale language ---
    for marker in sale_markers():
        if _phrase_pattern(marker).search(text_l):
            sig.has_sale_language = True
            break

    # --- price detection (numbers/$/€) ---
    prices: list[int] = []
    for m in _DOLLAR.finditer(text_l):
        try:
            prices.append(int(re.search(r"\d+", m.group()).group()))
        except (AttributeError, ValueError):
            pass
    for m in _EURO.finditer(text_l):
        try:
            prices.append(int(re.search(r"\d+", m.group()).group()))
        except (AttributeError, ValueError):
            pass
    # Bare 2-4 digit numbers also count as a price hint (story scope is loose).
    # Skip if it's clearly a year like 1990, 2024 — drop 4-digit numbers > 1900.
    for m in _DIGITS_2_4.finditer(text_l):
        val = int(m.group())
        if 1900 <= val <= 2100:
            continue
        prices.append(val)

    # de-dupe preserving order
    seen: set[int] = set()
    sig.prices = [p for p in prices if not (p in seen or seen.add(p))]
    sig.has_price = len(sig.prices) > 0

    # --- disqualifiers ---
    if _SOLD_WORD.search(text_l):
        sig.has_sold = True
    for phrase in offer_disqualifiers():
        if _phrase_pattern(phrase).search(text_l):
            sig.has_offer_only = True
            break

    # --- snippet for UI (first 140 chars, single line) ---
    sig.snippet = " ".join(text.split())[:140]

    return sig


def classify(sig: StorySignal) -> Classification:
    """Disqualifier wins over positive. Sold-out wins over offers-only."""
    if sig.has_sold:
        return "sold"
    if sig.has_offer_only:
        return "offers_only"
    if sig.has_sale_language or sig.has_price:
        return "active_sale"
    return "none"
