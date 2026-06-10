"""Unit tests for ingest/extract.py — menswear/archive dict edition.

Covers the same E# matrix as before, retargeted to the new clothing+brand dicts:
  clothing: menswear/unisex only (hoodie, jacket, jeans, sneakers, ...)
  brands:   archive list (Rick Owens, Maison Margiela, Raf Simons, ...)
"""

from __future__ import annotations

import pytest

from storysale.ingest.extract import (
    Extracted,
    extract,
    find_brands,
    find_clothing,
    find_prices,
    find_sold,
)


# ---------- E1–E11: combined extract cases ----------

def test_E1_brand_clothing_dollar_price():
    ex = extract("rick owens hoodie $250")
    assert ex.prices == [250]
    assert "hoodie" in ex.clothing
    assert ex.brands == ["Rick Owens"]
    assert ex.sold is False


def test_E2_brand_alias_and_asking_price():
    ex = extract("margiela jacket, asking 400")
    assert ex.prices == [400]
    assert "jacket" in ex.clothing
    assert ex.brands == ["Maison Margiela"]


def test_E3_cdg_alias_and_dollars_suffix():
    ex = extract("cdg shirt 350 dollars")
    assert ex.prices == [350]
    assert "shirt" in ex.clothing
    assert ex.brands == ["Comme des Garçons"]


def test_E5_adjacent_two_digit_price():
    ex = extract("jeans 80")
    assert ex.prices == [80]
    assert "jeans" in ex.clothing


def test_E6_phone_number_not_a_price():
    ex = extract("call 4155551234")
    assert ex.prices == []


def test_E7_sold_marker_in_caption():
    ex = extract("hoodie $200 SOLD")
    assert ex.sold is True
    assert ex.prices == [200]


def test_E8_pending_and_ppu_both_count_as_sold():
    assert find_sold("jacket $300 pending") is True
    assert find_sold("jacket $300 ppu") is True
    assert find_sold("jacket $300 on hold") is True


def test_E9_emoji_lock_marks_sold():
    ex = extract("🔒 jeans $90")
    assert ex.sold is True


def test_E10_drop_announcement_no_clothing_no_price():
    ex = extract("DROP TONIGHT 8PM")
    assert ex.clothing == []
    assert ex.prices == []
    assert ex.sold is False


def test_E11_case_insensitive():
    ex = extract("HOODIE $200")
    assert ex.prices == [200]
    assert "hoodie" in ex.clothing


def test_E12_empty_and_none_do_not_crash():
    assert extract("", None) == Extracted()
    assert extract("", "") == Extracted()
    assert find_prices("") == []
    assert find_clothing("") == []
    assert find_brands("") == []
    assert find_sold("") is False


# ---------- Additional safety nets ----------

def test_longest_brand_alias_wins():
    """'maison martin margiela' (alias) should map to canonical 'Maison Margiela'
    once, not double-count with shorter alias 'margiela'."""
    ex = extract("maison martin margiela hoodie $400")
    assert ex.brands == ["Maison Margiela"]


def test_percent_off_is_not_a_price():
    ex = extract("hoodie 30% off")
    assert ex.prices == []


def test_adjacent_requires_clothing_in_window():
    """A 2-4 digit number with no clothing context shouldn't trigger."""
    assert find_prices("just chilling at the park 25 minutes") == []


def test_three_digit_adjacent_price():
    ex = extract("vintage leather jacket 150")
    assert 150 in ex.prices
    # longest-match-wins: "leather jacket" beats bare "jacket"
    assert "leather jacket" in ex.clothing


def test_dollar_sign_with_small_amount():
    ex = extract("tee $5")
    assert ex.prices == [5]


def test_ocr_combines_with_caption():
    ex = extract(caption="cute jeans", ocr_text="$45 dm to claim")
    assert "jeans" in ex.clothing
    assert ex.prices == [45]


def test_two_brands_in_one_caption():
    ex = extract("rick owens hoodie paired with margiela boots, $400")
    assert set(ex.brands) == {"Rick Owens", "Maison Margiela"}


# ---------- Dropped women-coded terms should NOT match ----------

def test_skirt_no_longer_in_dict():
    assert find_clothing("denim skirt $30") == []


def test_dress_no_longer_in_dict():
    assert find_clothing("summer dress $40") == []


def test_top_no_longer_in_dict():
    """'top' was women-coded and removed."""
    assert find_clothing("cute top $20") == []


# ---------- Dropped brands should NOT match ----------

def test_brandy_melville_no_longer_in_dict():
    assert find_brands("brandy melville haul") == []


def test_nike_no_longer_in_dict():
    assert find_brands("nike air max") == []
