"""Deterministic entity extraction: prices, clothing terms, brands, sold flag.

All matching is case-insensitive. Pure functions, no I/O — safe to unit test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..dicts import brand_aliases, clothing_terms, sold_markers


@dataclass
class Extracted:
    prices: list[int] = field(default_factory=list)
    clothing: list[str] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    sold: bool = False


_PRICE_DOLLAR = re.compile(r"\$\s*(\d{1,4})(?!\d|%)")
_PRICE_SUFFIX = re.compile(r"(?<!\d)(\d{1,4})\s*(?:dollars?|usd)\b", re.I)
_PRICE_QUALIFIER = re.compile(
    r"\b(?:asking|selling\s+for|going\s+for|price\s*:?\s*|for)\s+(\d{1,4})(?!\d|%)",
    re.I,
)
_ADJACENT_DIGITS = re.compile(r"\d{2,4}")


def _alias_pattern(s: str) -> re.Pattern[str]:
    """Word-boundary-ish match that also works for tokens with non-word chars (e.g. 'h&m')."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", re.I)


def find_brands(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    aliases = brand_aliases()
    # Longest alias first so "brandy melville" wins over "brandy".
    found: list[str] = []
    seen: set[str] = set()
    masked = text_l
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        pat = _alias_pattern(alias)
        if pat.search(masked):
            canonical = aliases[alias]
            if canonical not in seen:
                found.append(canonical)
                seen.add(canonical)
            masked = pat.sub(" " * (len(alias) + 2), masked)
    return found


def find_clothing(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    found: list[str] = []
    seen: set[str] = set()
    masked = text_l
    for term in sorted(clothing_terms(), key=len, reverse=True):
        pat = _alias_pattern(term)
        if pat.search(masked):
            if term not in seen:
                found.append(term)
                seen.add(term)
            masked = pat.sub(" " * (len(term) + 2), masked)
    return found


def find_sold(text: str) -> bool:
    if not text:
        return False
    text_l = text.lower()
    for marker in sold_markers():
        if any(c.isalpha() for c in marker):
            if _alias_pattern(marker).search(text_l):
                return True
        else:
            # Emoji or symbol — substring match is fine.
            if marker in text_l:
                return True
    return False


def find_prices(text: str) -> list[int]:
    if not text:
        return []
    text_l = text.lower()
    out: list[int] = []

    for m in _PRICE_DOLLAR.finditer(text_l):
        out.append(int(m.group(1)))
    for m in _PRICE_SUFFIX.finditer(text_l):
        out.append(int(m.group(1)))
    for m in _PRICE_QUALIFIER.finditer(text_l):
        out.append(int(m.group(1)))

    # Adjacency rule: a 2-4 digit standalone number within 3 tokens of a clothing term.
    masked = text_l
    for term in sorted(clothing_terms(), key=len, reverse=True):
        masked = _alias_pattern(term).sub(" __CLOTHING__ ", masked)
    tokens = re.findall(r"\S+", masked)
    for i, tok in enumerate(tokens):
        if not re.fullmatch(r"\d{2,4}", tok):
            continue
        window = tokens[max(0, i - 3) : i] + tokens[i + 1 : i + 4]
        if "__CLOTHING__" in window:
            out.append(int(tok))

    # De-dupe preserving order.
    seen: set[int] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def extract(caption: str, ocr_text: Optional[str] = None) -> Extracted:
    """Combine caption + OCR text and run all extractors over the union."""
    text = " ".join(t for t in (caption, ocr_text) if t)
    return Extracted(
        prices=find_prices(text),
        clothing=find_clothing(text),
        brands=find_brands(text),
        sold=find_sold(text),
    )
