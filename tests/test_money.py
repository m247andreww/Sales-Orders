"""Money shown to people is whole pounds, rounded half up (CFO, 2026-10-02). All figures synthetic."""

from __future__ import annotations

from decimal import Decimal

from sales_orders.money import gbp


def test_whole_pounds_rounded_half_up() -> None:
    assert gbp(Decimal("87972.74")) == "£87,973"
    assert gbp(Decimal("2.50")) == "£3"  # half up; Python's default (half even) would give £2
    assert gbp(Decimal("17594.49")) == "£17,594"
    assert gbp(Decimal(1200000)) == "£1,200,000"


def test_missing_amount_text() -> None:
    assert gbp(None) == "N/A"
    assert gbp(None, "no limit reported") == "no limit reported"
