"""A same-day CFO decision is not re-opened by new figures that tell the CFO nothing new (migration 0025).

All companies, names and figures are synthetic.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import CFO, act_as

from sales_orders import credit
from sales_orders.credit import BureauFigure
from sales_orders.credit_arr import ArrLine, ParsedArrFile
from sales_orders.db import Connection
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data

NEXT_WEEK = date.today() + timedelta(days=7)


def _arr(conn: Connection, revenue: str, sha: str) -> None:
    credit.load_arr_snapshot(
        conn,
        ParsedArrFile(
            source_name=f"ARR {sha}.xlsx",
            sha256=sha * 64,
            lines=(ArrLine(1, "Acme Widgets", "ACM001", "Live", "monthly", Decimal(revenue)),),
            skipped=(),
        ),
    )


def _decided_on_experian_zero(conn: Connection) -> None:
    """Like Napier Parking: Experian shows £0, the CFO decides the same day."""
    load_master_data(
        conn,
        MasterDataIn(
            customers=(CustomerIn(legal_name="Acme Widgets", arr_prefix="ACM"),),
            credit_subjects=(
                CreditSubjectIn(
                    display_name="Acme Widgets",
                    relationship="customer",
                    customer_legal_name="Acme Widgets",
                    company_number="01234567",
                ),
            ),
        ),
    )
    _arr(conn, "12000.00", "a")
    act_as(conn, CFO)
    credit.enter_bureau_figures(conn, "Acme Widgets", {"experian": BureauFigure(Decimal("0"))})
    first = credit.assess(conn, "Acme Widgets")
    assert first["outcome_code"] == "cfo_review"
    credit.decide(conn, "Acme Widgets", first["recommended_limit"], "trade at risk", NEXT_WEEK)


def _latest(conn: Connection) -> dict[str, object]:
    row = conn.execute(
        """SELECT a.outcome_code, a.held_by_credit_limit_id IS NOT NULL AS held
             FROM sales.credit_assessment a ORDER BY a.credit_assessment_id DESC LIMIT 1"""
    ).fetchone()
    assert row is not None
    return dict(row)


def test_new_figure_with_nothing_new_keeps_todays_decision(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _decided_on_experian_zero(conn)
    credit.enter_bureau_figures(conn, "Acme Widgets", {"creditsafe": BureauFigure(Decimal("86000"))})
    credit.assess(conn, "Acme Widgets")
    assert _latest(conn) == {"outcome_code": "override_in_force", "held": True}
    review = credit.desk_export(conn, credit.datetime(2026, 1, 1, tzinfo=credit.LONDON))["review"]
    assert "Acme Widgets" not in [r["company"] for r in review]


def test_a_new_reason_still_goes_to_the_cfo(conn: Connection, master_data: MasterDataIn) -> None:
    _decided_on_experian_zero(conn)
    credit.enter_bureau_figures(
        conn, "Acme Widgets", {"experian": BureauFigure(Decimal("0"), "Maximum Risk")}
    )
    credit.assess(conn, "Acme Widgets")
    assert _latest(conn) == {"outcome_code": "cfo_review", "held": False}


def test_a_changed_requirement_still_goes_to_the_cfo(conn: Connection, master_data: MasterDataIn) -> None:
    _decided_on_experian_zero(conn)
    _arr(conn, "48000.00", "b")
    credit.enter_bureau_figures(conn, "Acme Widgets", {"creditsafe": BureauFigure(Decimal("86000"))})
    credit.assess(conn, "Acme Widgets")
    assert _latest(conn) == {"outcome_code": "cfo_review", "held": False}
