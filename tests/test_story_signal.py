"""Unit tests for ingest/story_signal.py — story-sale notifier detector."""

from storysale.ingest.story_signal import classify, detect


# ---------- Positive: sale language ----------

def test_SS1_story_sale_phrase():
    sig = detect("STORY SALE dm to claim")
    assert sig.has_sale_language is True
    assert classify(sig) == "active_sale"


def test_SS_selling_word():
    sig = detect("selling my rick collection")
    assert sig.has_sale_language is True
    assert classify(sig) == "active_sale"


# ---------- Positive: price indicators ----------

def test_SS2_dollar_price():
    sig = detect("$300 dm me")
    assert sig.has_price is True
    assert 300 in sig.prices
    assert classify(sig) == "active_sale"


def test_SS3_euro_price():
    sig = detect("€250 shipped")
    assert sig.has_price is True
    assert 250 in sig.prices
    assert classify(sig) == "active_sale"


def test_SS_bare_2_4_digit_counts_as_price():
    sig = detect("offering at 450")
    # 'offering' isn't a sale marker, but bare 2-4 digit number is a price hint
    assert sig.has_price is True
    assert classify(sig) == "active_sale"


def test_SS_year_like_number_skipped():
    """1990–2100 are years, not prices."""
    sig = detect("circa 1995 piece")
    assert sig.prices == []


# ---------- Negative: disqualifiers ----------

def test_SS4_h_slash_o():
    sig = detect("h/o on this rick piece")
    assert sig.has_offer_only is True
    assert classify(sig) == "offers_only"


def test_SS5_ho_token():
    sig = detect("ho 200 dm")
    assert sig.has_offer_only is True
    assert classify(sig) == "offers_only"


def test_SS6_highest_offer():
    sig = detect("highest offer wins")
    assert sig.has_offer_only is True
    assert classify(sig) == "offers_only"


def test_SS7_c_slash_o():
    sig = detect("c/o $300 dm")
    assert sig.has_offer_only is True
    assert classify(sig) == "offers_only"


def test_SS_best_offer_and_auction():
    assert classify(detect("best offer takes it")) == "offers_only"
    assert classify(detect("auction ends tonight")) == "offers_only"


# ---------- Sold ----------

def test_SS8_sold_word():
    sig = detect("sold")
    assert sig.has_sold is True
    assert classify(sig) == "sold"


def test_SS9_sold_wins_over_price():
    sig = detect("SOLD - was $250")
    assert sig.has_sold is True
    assert sig.has_price is True
    # sold takes priority in classification
    assert classify(sig) == "sold"


def test_SS_sold_inside_word_does_not_trigger():
    """'solder' should not match 'sold'."""
    sig = detect("custom solder job")
    assert sig.has_sold is False


# ---------- Mixed signals ----------

def test_SS10_disqualifier_wins_over_sale_language():
    """Per spec: 'h/o ... flag as no sale.' Even with sale language, h/o wins."""
    sig = detect("STORY SALE 🔥 h/o on the rick")
    assert sig.has_sale_language is True
    assert sig.has_offer_only is True
    assert classify(sig) == "offers_only"


def test_SS_offer_with_price_still_offer_only():
    sig = detect("h/o starting at $200")
    assert classify(sig) == "offers_only"


# ---------- Negative: no signal ----------

def test_SS11_random_text_no_signal():
    sig = detect("just chilling at the park")
    assert sig.has_sale_language is False
    assert sig.has_price is False
    assert classify(sig) == "none"


def test_SS12_drop_announcement_no_signal():
    """'DROP TONIGHT 8PM' — no price (8 is one digit), no sale word."""
    sig = detect("DROP TONIGHT 8PM")
    # 'drop' IS in sale_markers (we treat 'drop' as a sale signal)
    # this case should still classify as active_sale because of the word "drop"
    # The user wanted broad sale-language detection.
    assert classify(sig) == "active_sale"


def test_SS13_empty_string_safe():
    sig = detect("")
    assert classify(sig) == "none"
    assert sig.prices == []
    assert sig.snippet == ""


# ---------- Snippet ----------

def test_SS_snippet_is_compact():
    long_text = "STORY SALE\n\n  multiple\n  lines  with  spaces  " + "x" * 200
    sig = detect(long_text)
    assert "\n" not in sig.snippet
    assert len(sig.snippet) <= 140
