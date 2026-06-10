"""Unit tests for the keep/drop gate."""

from storysale.ingest.extract import Extracted
from storysale.ingest.filter import gate


def _ex(clothing=(), prices=(), brands=(), sold=False) -> Extracted:
    return Extracted(
        clothing=list(clothing),
        prices=list(prices),
        brands=list(brands),
        sold=sold,
    )


def test_F1_clothing_and_price_keeps():
    r = gate(_ex(clothing=["top"], prices=[25]))
    assert r.decision == "keep"
    assert r.reason == "ok"


def test_F2_no_clothing_rejects():
    r = gate(_ex(prices=[25]))
    assert r.decision == "reject"
    assert r.reason == "no_clothing"


def test_F3_no_price_rejects():
    r = gate(_ex(clothing=["top"]))
    assert r.decision == "reject"
    assert r.reason == "no_price"


def test_F4_sold_overrides_keep():
    r = gate(_ex(clothing=["top"], prices=[25], sold=True))
    assert r.decision == "reject"
    assert r.reason == "sold"


def test_F5_drop_announcement_falls_to_no_clothing():
    """An item with no clothing and no price (like 'DROP TONIGHT 8PM') reports
    the first failing condition. Specific reason matters less than the rejection."""
    r = gate(_ex())
    assert r.decision == "reject"
    assert r.reason in ("no_clothing",)  # checked before no_price by design
