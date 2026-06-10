"""Tests for the OCR wrapper. Bypass real easyocr — use from_raw_tokens."""

from storysale.ingest.ocr import CONF_THRESHOLD, from_raw_tokens


def test_O1_low_confidence_tokens_flag_review_and_drop_text():
    r = from_raw_tokens([("Sk1rt", 0.4), ("$20", 0.95)])
    assert r.text == "$20"
    assert r.needs_review is True


def test_O2_high_confidence_only_no_review():
    r = from_raw_tokens([("BRANDY", 0.92), ("$25", 0.95)])
    assert "BRANDY" in r.text
    assert "$25" in r.text
    assert r.needs_review is False


def test_empty_tokens():
    r = from_raw_tokens([])
    assert r.text == ""
    assert r.needs_review is False


def test_threshold_is_inclusive_at_boundary():
    r = from_raw_tokens([("foo", CONF_THRESHOLD)])
    assert r.text == "foo"
    assert r.needs_review is False
