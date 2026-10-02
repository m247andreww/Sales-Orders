"""How money is SHOWN to people: whole pounds (CFO, 2026-10-02: "lose the dp from the outputs").

Calculations, storage and JSON keep exact pence (numeric / Decimal). Only text for people is rounded,
half up (£0.50 -> £1), so a column of rounded figures can differ from its rounded total by £1.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def gbp(value: Any, missing: str = "N/A") -> str:
    """£1,234 for Decimal("1234.49"); `missing` for None."""
    if value is None:
        return missing
    return f"£{Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP):,}"
