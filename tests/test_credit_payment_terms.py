"""Payment terms in the credit process (migration 0026). All companies, names and figures are synthetic."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import CFO, FINANCE, act_as, savepoint_rejects

from sales_orders import credit
from sales_orders.credit import PaymentTerms
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data

STOPFORD_LIKE = PaymentTerms(30, "direct_debit", 14)  # recurring on DD at the standard 30; one-off 14 days


def _setup(conn: Connection) -> int:
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
    row = conn.execute("SELECT customer_id FROM sales.customer WHERE legal_name = 'Acme Widgets'").fetchone()
    assert row is not None
    return int(row["customer_id"])


def _terms(conn: Connection, customer_id: int) -> dict[str, object]:
    t = credit.customer_terms(conn, customer_id)
    return {k: t[k] for k in ("recurring", "one_off", "standard", "default")}


def test_a_customer_with_no_terms_is_on_the_standard_30_days(
    conn: Connection, master_data: MasterDataIn
) -> None:
    cid = _setup(conn)
    assert _terms(conn, cid) == {
        "recurring": "30 days",
        "one_off": "30 days",
        "standard": True,
        "default": True,
    }


def test_non_standard_terms_are_recorded_with_reason_and_approver(
    conn: Connection, master_data: MasterDataIn
) -> None:
    cid = _setup(conn)
    act_as(conn, CFO)
    credit.set_payment_terms(conn, "Acme Widgets", STOPFORD_LIKE, "14 days unless payable by Direct Debit")
    assert _terms(conn, cid) == {
        "recurring": "30 days (Direct Debit)",
        "one_off": "14 days",
        "standard": False,
        "default": False,
    }
    row = conn.execute(
        "SELECT approved_by_employee_id IS NOT NULL AS approved, reason FROM sales.customer_credit_terms"
        " WHERE customer_id = %s",
        (cid,),
    ).fetchone()
    assert row == {"approved": True, "reason": "14 days unless payable by Direct Debit"}


def test_non_standard_terms_need_a_reason(conn: Connection, master_data: MasterDataIn) -> None:
    cid = _setup(conn)
    act_as(conn, CFO)
    msg = savepoint_rejects(
        conn, "SELECT sales.set_customer_payment_terms(%s, 30, 'direct_debit', 14, false, NULL)", (cid,)
    )
    assert "need a reason" in msg


def test_only_the_credit_approver_may_set_terms(conn: Connection, master_data: MasterDataIn) -> None:
    cid = _setup(conn)
    act_as(conn, FINANCE)
    msg = savepoint_rejects(
        conn, "SELECT sales.set_customer_payment_terms(%s, 30, 'bank_transfer', 30, false, NULL)", (cid,)
    )
    assert "setting payment terms is not permitted" in msg


def test_changing_terms_closes_the_earlier_period_and_keeps_its_approver(
    conn: Connection, master_data: MasterDataIn
) -> None:
    cid = _setup(conn)
    act_as(conn, CFO)
    # an earlier non-standard period, approved by the CFO, started last month
    credit.set_payment_terms(conn, "Acme Widgets", PaymentTerms(7, "bank_transfer", 7), "new client")
    conn.execute(
        "ALTER TABLE sales.customer_credit_terms DISABLE TRIGGER credit_terms_authority"
    )  # test setup only
    conn.execute(
        "UPDATE sales.customer_credit_terms SET effective_from = current_date - 30, approved_at = %s"
        " WHERE customer_id = %s",
        (date(2026, 1, 2), cid),
    )
    conn.execute("ALTER TABLE sales.customer_credit_terms ENABLE TRIGGER credit_terms_authority")
    credit.set_payment_terms(conn, "Acme Widgets", STOPFORD_LIKE, "14 days unless payable by Direct Debit")
    rows = conn.execute(
        """SELECT effective_from = current_date - 30 AS old, effective_to, recurring_terms_days, approved_at::date AS a
             FROM sales.customer_credit_terms WHERE customer_id = %s ORDER BY effective_from""",
        (cid,),
    ).fetchall()
    assert [(r["old"], r["effective_to"], r["recurring_terms_days"]) for r in rows] == [
        (True, date.today() - timedelta(days=1), 7),
        (False, None, 30),
    ]
    assert rows[0]["a"] == date(2026, 1, 2)  # closing the period did not re-stamp who approved it


def test_a_second_change_today_amends_todays_terms(conn: Connection, master_data: MasterDataIn) -> None:
    cid = _setup(conn)
    act_as(conn, CFO)
    credit.set_payment_terms(conn, "Acme Widgets", STOPFORD_LIKE, "first")
    credit.set_payment_terms(conn, "Acme Widgets", PaymentTerms(30, "bank_transfer", 30), None)
    assert _terms(conn, cid)["standard"] is True
    n = conn.execute(
        "SELECT count(*) AS n FROM sales.customer_credit_terms WHERE customer_id = %s", (cid,)
    ).fetchone()
    assert n == {"n": 1}


def test_a_decision_can_set_terms_with_the_limit(conn: Connection, master_data: MasterDataIn) -> None:
    cid = _setup(conn)
    act_as(conn, CFO)
    credit.decide(
        conn,
        "Acme Widgets",
        Decimal("8000"),
        "pays by DD",
        date.today() + timedelta(days=7),
        terms=STOPFORD_LIKE,
    )
    assert _terms(conn, cid)["one_off"] == "14 days"


@pytest.mark.parametrize(
    ("terms", "error"),
    [
        (PaymentTerms(181, "bank_transfer", 30), "0 to 180"),
        (PaymentTerms(30, "bank_transfer", -1), "0 to 180"),
        (PaymentTerms(30, "cheque", 30), "unknown payment method"),
    ],
)
def test_python_refuses_bad_terms(
    conn: Connection, master_data: MasterDataIn, terms: PaymentTerms, error: str
) -> None:
    _setup(conn)
    act_as(conn, CFO)
    with pytest.raises(SalesOrderError, match=error):
        credit.set_payment_terms(conn, "Acme Widgets", terms, "x")


def test_register_shows_terms_but_never_decision_reasons(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    act_as(conn, CFO)
    credit.set_payment_terms(conn, "Acme Widgets", STOPFORD_LIKE, "confidential reason")
    reg = credit.register_export(conn)
    acme = [c for c in reg["customers"] if c["customer"] == "Acme Widgets"]
    assert [(c["terms"]["one_off"], c["status"]) for c in acme] == [("14 days", "no_limit")]
    assert "confidential reason" not in str(reg)
    assert reg["standard_terms_days"] == 30
