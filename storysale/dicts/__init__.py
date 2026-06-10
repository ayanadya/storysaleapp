"""Dictionary loaders. Files live next to this module."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_DICT_DIR = Path(__file__).parent


def _load_lines(name: str) -> list[str]:
    raw = (_DICT_DIR / name).read_text(encoding="utf-8").splitlines()
    return [
        line.strip().lower()
        for line in raw
        if line.strip() and not line.lstrip().startswith("#")
    ]


@lru_cache(maxsize=1)
def clothing_terms() -> list[str]:
    return _load_lines("clothing.txt")


@lru_cache(maxsize=1)
def sold_markers() -> list[str]:
    return _load_lines("sold_markers.txt")


@lru_cache(maxsize=1)
def sale_markers() -> list[str]:
    """Positive sale-language signals — story pipeline only."""
    return _load_lines("sale_markers.txt")


@lru_cache(maxsize=1)
def offer_disqualifiers() -> list[str]:
    """Phrases that flag a story as offer-thread (not a fixed-price sale)."""
    return _load_lines("offer_disqualifiers.txt")


@lru_cache(maxsize=1)
def brand_aliases() -> dict[str, str]:
    """Return {alias_lower: canonical_name}. Includes each canonical as its own alias."""
    raw = yaml.safe_load((_DICT_DIR / "brands.yaml").read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for canonical, aliases in raw.items():
        canonical = canonical.strip()
        out[canonical.lower()] = canonical
        for alias in aliases or []:
            out[alias.strip().lower()] = canonical
    return out
