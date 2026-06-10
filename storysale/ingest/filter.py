"""The keep/drop gate. Pure function over an Extracted record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .extract import Extracted

Decision = Literal["keep", "reject"]
Reason = Literal["no_clothing", "no_price", "sold", "ok"]


@dataclass
class GateResult:
    decision: Decision
    reason: Reason


def gate(ex: Extracted) -> GateResult:
    """Keep iff clothing>=1 AND price>=1 AND not sold. Reasons are mutually exclusive,
    checked sold-first so rejected-sold doesn't get reported as missing-price."""
    if ex.sold:
        return GateResult("reject", "sold")
    if not ex.clothing:
        return GateResult("reject", "no_clothing")
    if not ex.prices:
        return GateResult("reject", "no_price")
    return GateResult("keep", "ok")
