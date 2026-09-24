"""Database-level constraints: these hold even if someone bypasses the application."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import load, savepoint_rejects
from psycopg import sql

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _order_id(conn: Connection, order_json: dict[str, Any]) -> int:
    return int(service.order_summary(conn, load(conn, order_json).order_number)["sales_order_id"])


def _insert_line(conn: Connection, order_id: int, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "line_number": 99,
        "description": "x",
        "line_category_code": "hardware",
        "supplier_id": conn.execute("SELECT min(supplier_id) AS id FROM sales.supplier").fetchone()["id"],  # type: ignore[index]
        "quantity": 1,
        "billing_frequency_code": "one_off",
        "billing_periods": 1,
        "cost_currency_code": "GBP",
        "unit_cost_in_cost_currency": 1,
        "unit_sell": 2,
        "fx_rate_id": None,
    }
    values.update(overrides)
    statement = sql.SQL("INSERT INTO sales.sales_order_line ({}) VALUES ({})").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in ["sales_order_id", *values]),
        sql.SQL(", ").join(sql.Placeholder() * (len(values) + 1)),
    )
    return savepoint_rejects(conn, statement, (order_id, *values.values()))


@pytest.fixture
def order_id(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> int:
    return _order_id(conn, order_json)


def test_one_off_line_must_have_one_period(conn: Connection, order_id: int) -> None:
    assert "sales_order_line_check" in _insert_line(conn, order_id, billing_periods=12)


def test_foreign_currency_cost_requires_fx_rate(conn: Connection, order_id: int) -> None:
    assert "needs an FX rate" in _insert_line(conn, order_id, cost_currency_code="USD")


def test_fx_rate_must_match_currencies(conn: Connection, order_id: int) -> None:
    fx_id = conn.execute("SELECT fx_rate_id FROM sales.fx_rate").fetchone()["fx_rate_id"]  # type: ignore[index]
    assert "expected EUR->GBP" in _insert_line(conn, order_id, cost_currency_code="EUR", fx_rate_id=fx_id)


def test_fx_rate_not_allowed_for_same_currency(conn: Connection, order_id: int) -> None:
    fx_id = conn.execute("SELECT fx_rate_id FROM sales.fx_rate").fetchone()["fx_rate_id"]  # type: ignore[index]
    assert "equals order currency" in _insert_line(conn, order_id, fx_rate_id=fx_id)


def test_quantity_must_be_positive(conn: Connection, order_id: int) -> None:
    assert "quantity_check" in _insert_line(conn, order_id, quantity=0)


def test_supplier_quote_must_belong_to_line_supplier(conn: Connection, order_id: int) -> None:
    quote_id = conn.execute("SELECT supplier_quote_id FROM sales.supplier_quote").fetchone()[
        "supplier_quote_id"
    ]  # type: ignore[index]
    internal = conn.execute("SELECT supplier_id FROM sales.supplier WHERE is_internal").fetchone()[
        "supplier_id"
    ]  # type: ignore[index]
    assert "different supplier" in _insert_line(
        conn, order_id, supplier_id=internal, supplier_quote_id=quote_id
    )


def test_derived_money_cannot_be_typed_in(conn: Connection, order_id: int) -> None:
    msg = savepoint_rejects(
        conn, "UPDATE sales.sales_order_line SET net_sell = 1 WHERE sales_order_id = %s", (order_id,)
    )
    assert "can only be updated to DEFAULT" in msg


def test_credit_terms_cannot_overlap(conn: Connection, master_data: MasterDataIn) -> None:
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.customer_credit_terms (customer_id, effective_from, recurring_terms_days,
               recurring_payment_method_code, one_off_terms_days, risk_rating_code)
           SELECT customer_id, DATE '2026-06-01', 14, 'direct_debit', 0, 'high' FROM sales.customer""",
    )
    assert "customer_credit_terms_no_overlap" in msg


def test_non_standard_terms_need_reason_and_approver(conn: Connection, master_data: MasterDataIn) -> None:
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.customer_credit_terms (customer_id, effective_from, effective_to,
               recurring_terms_days, recurring_payment_method_code, one_off_terms_days, risk_rating_code,
               is_non_standard)
           SELECT customer_id, DATE '2025-01-01', DATE '2025-12-31', 60, 'bank_transfer', 60, 'standard', true
             FROM sales.customer""",
    )
    assert "customer_credit_terms_check" in msg


def test_audit_log_is_append_only(conn: Connection, master_data: MasterDataIn) -> None:
    assert "append-only" in savepoint_rejects(conn, "UPDATE audit.change_log SET changed_by = 'someone else'")
    assert "append-only" in savepoint_rejects(conn, "DELETE FROM audit.change_log")


def test_waived_check_needs_note(conn: Connection, order_id: int) -> None:
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_check SET check_status_code = 'waived', notes = NULL,
                  checked_by_employee_id = (SELECT min(employee_id) FROM sales.employee), checked_at = now()
            WHERE sales_order_id = %s""",
        (order_id,),
    )
    assert "sales_order_check_check" in msg
