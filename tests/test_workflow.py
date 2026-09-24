"""Status workflow, approval gate and locking."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn

FINANCE = "test.finance@example.com"


def _pass_all_checks(conn: Connection, order_number: str) -> None:
    for c in service.order_checks(conn, order_number):
        service.record_check(
            conn,
            order_number,
            check_type=c["check_type_code"],
            status="passed",
            checked_by_email=FINANCE,
            notes="evidence seen",
        )


def _approved_order(conn: Connection, order_json: dict[str, Any]) -> str:
    number = load(conn, order_json).order_number
    service.change_status(conn, number, "validated", "reviewed")
    _pass_all_checks(conn, number)
    service.change_status(conn, number, "approved", "all checks passed")
    return number


def _status_change_fails(conn: Connection, number: str, to_status: str) -> str:
    with pytest.raises(psycopg.Error) as info, conn.transaction():
        service.change_status(conn, number, to_status, "attempt")
    return str(info.value.diag.message_primary)


def test_cannot_skip_validation(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    assert "is not allowed" in _status_change_fails(conn, number, "approved")


def test_approval_blocked_while_checks_pending(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    service.change_status(conn, number, "validated", "reviewed")
    assert "checks are unresolved" in _status_change_fails(conn, number, "approved")


def test_approval_blocked_by_error_exception(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][5]["unit_sell"] = "50.00"  # mark up the sales-tax pass-through line
    order_json.pop("stated_totals")
    number = load(conn, order_json).order_number
    service.change_status(conn, number, "validated", "reviewed")
    _pass_all_checks(conn, number)
    assert "error-level exceptions" in _status_change_fails(conn, number, "approved")


def test_happy_path_records_history_with_reasons(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _approved_order(conn, order_json)
    service.change_status(conn, number, "provisioning", "supplier orders placed")
    service.change_status(conn, number, "invoiced", "Xero invoice raised")
    service.change_status(conn, number, "closed", "complete")
    history = conn.execute(
        """SELECT h.from_status, h.to_status, h.reason FROM sales.sales_order_status_history h
             JOIN sales.sales_order o USING (sales_order_id)
            WHERE o.order_number = %s ORDER BY h.sales_order_status_history_id""",
        (number,),
    ).fetchall()
    assert [(h["from_status"], h["to_status"], h["reason"]) for h in history] == [
        (None, "received", "created"),
        ("received", "validated", "reviewed"),
        ("validated", "approved", "all checks passed"),
        ("approved", "provisioning", "supplier orders placed"),
        ("provisioning", "invoiced", "Xero invoice raised"),
        ("invoiced", "closed", "complete"),
    ]


def test_lines_locked_after_approval(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _approved_order(conn, order_json)
    order_id = service.order_summary(conn, number)["sales_order_id"]
    msg = savepoint_rejects(
        conn, "UPDATE sales.sales_order_line SET unit_sell = 1 WHERE sales_order_id = %s", (order_id,)
    )
    assert f"order {number} is in status approved and its lines are locked" == msg
    msg = savepoint_rejects(conn, "DELETE FROM sales.sales_order_line WHERE sales_order_id = %s", (order_id,))
    assert "lines are locked" in msg


def test_commercial_header_locked_after_approval(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _approved_order(conn, order_json)
    msg = savepoint_rejects(
        conn,
        "UPDATE sales.sales_order SET margin_exception_reason = 'late change' WHERE order_number = %s",
        (number,),
    )
    assert "commercial fields are locked" in msg


def test_on_hold_forces_revalidation(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _approved_order(conn, order_json)
    service.change_status(conn, number, "on_hold", "customer query")
    assert "is not allowed" in _status_change_fails(conn, number, "approved")
    service.change_status(conn, number, "received", "query resolved")


def test_status_change_requires_reason(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    with pytest.raises(ValueError, match="reason"):
        service.change_status(conn, number, "validated", "  ")


def test_orders_must_be_created_as_received(conn: Connection, master_data: MasterDataIn) -> None:
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.sales_order (customer_id, title, order_type_code, status_code, received_at,
                                          submitted_by_employee_id)
           SELECT customer_id, 'x', 'new', 'approved', now(), (SELECT min(employee_id) FROM sales.employee)
             FROM sales.customer LIMIT 1""",
    )
    assert "must start in status received" in msg
