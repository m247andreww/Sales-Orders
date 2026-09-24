"""House and Legacy accounts, and the leaver routine (CFO, 2026-09-24)."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
import pytest
from conftest import load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _rules(conn: Connection, order_json: dict[str, Any]) -> list[tuple[str, str]]:
    number = load(conn, order_json).order_number
    return [(e["rule_code"], e["message"]) for e in service.order_exceptions(conn, number)]


def _make_house(conn: Connection, label: str) -> None:
    conn.execute(
        "UPDATE sales.customer_account_allocation SET employee_id = NULL, house_account = %s", (label,)
    )


@pytest.mark.parametrize("label", ["House", "Legacy"])
def test_house_and_legacy_orders_cannot_be_net_new(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any], label: str
) -> None:
    _make_house(conn, label)
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN", account_manager_email=None)
    rules = _rules(conn, order_json)
    assert [r[0] for r in rules] == ["REPORTING_CATEGORY_MISMATCH"]
    assert f"the account is {label} (not allocated to a salesperson), so it cannot be net new" in rules[0][1]
    assert "expected EXPANSION_E" in rules[0][1]


def test_house_account_coded_existing_is_clean(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _make_house(conn, "House")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_E", account_manager_email=None)
    assert _rules(conn, order_json) == []


def test_salesperson_on_house_account_is_flagged(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _make_house(conn, "House")
    rules = _rules(conn, order_json)
    assert rules == [
        (
            "SALESPERSON_NOT_ACCOUNT_OWNER",
            "Order salesperson Test Territory Manager is not the account owner on 2026-09-09 (House)",
        )
    ]


def test_undefined_owner_types_are_refused(conn: Connection, master_data: MasterDataIn) -> None:
    """Auto Renew / Cust Success appear on the Register but are not yet defined by the CFO."""
    msg = savepoint_rejects(
        conn, "UPDATE sales.customer_account_allocation SET employee_id = NULL, house_account = 'Auto Renew'"
    )
    assert "customer_account_allocation_house_fk" in msg


def test_leaver_routine_moves_accounts_to_house(conn: Connection, master_data: MasterDataIn) -> None:
    assert service.employee_leaves(conn, "test.territory@example.com", date(2026, 10, 31)) == 1
    rows = conn.execute(
        """SELECT e.email, a.house_account, a.allocated_from, a.allocated_to, a.source
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
            ORDER BY a.allocated_from"""
    ).fetchall()
    assert rows == [
        {
            "email": "test.territory@example.com",
            "house_account": None,
            "allocated_from": date(2026, 1, 1),
            "allocated_to": date(2026, 10, 31),
            "source": "test fixture",
        },
        {
            "email": None,
            "house_account": "House",
            "allocated_from": date(2026, 11, 1),
            "allocated_to": None,
            "source": "Leaver: test.territory@example.com (last day 2026-10-31)",
        },
    ]
    active = conn.execute(
        "SELECT is_active FROM sales.employee WHERE email = 'test.territory@example.com'"
    ).fetchone()
    assert active == {"is_active": False}


def test_orders_before_leaving_keep_their_owner(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    service.employee_leaves(conn, "test.territory@example.com", date(2026, 10, 31))
    rules = [r[0] for r in _rules(conn, order_json)]  # order dated 9 Sep: owner still in post
    assert rules == ["OWNER_HAS_LEFT"]  # but the owner is now inactive: flagged for follow-up


def test_leaver_without_routine_is_flagged(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute("UPDATE sales.employee SET is_active = false WHERE email = 'test.territory@example.com'")
    assert [r[0] for r in _rules(conn, order_json)] == ["OWNER_HAS_LEFT"]


def test_unknown_leaver_rejected(conn: Connection, master_data: MasterDataIn) -> None:
    with pytest.raises(psycopg.Error, match="unknown employee"), conn.transaction():
        service.employee_leaves(conn, "nobody@example.com", date(2026, 10, 31))


def test_house_account_reallocated_to_new_salesperson(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    service.employee_leaves(conn, "test.territory@example.com", date(2026, 10, 31))
    service.allocate_account(conn, "Test Customer Ltd", "test.isam@example.com", date(2027, 1, 1), "CFO")
    periods = conn.execute(
        """SELECT COALESCE(e.email, a.house_account) AS owner, a.allocated_from, a.allocated_to
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
            ORDER BY a.allocated_from"""
    ).fetchall()
    assert [(p["owner"], p["allocated_from"], p["allocated_to"]) for p in periods] == [
        ("test.territory@example.com", date(2026, 1, 1), date(2026, 10, 31)),
        ("House", date(2026, 11, 1), date(2026, 12, 31)),
        ("test.isam@example.com", date(2027, 1, 1), None),
    ]


def test_new_salesperson_on_pre_existing_customer_is_existing(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """A customer inherited from House was pre-existing at allocation, so expansions are E, not NN."""
    service.employee_leaves(conn, "test.territory@example.com", date(2026, 1, 31))
    service.allocate_account(conn, "Test Customer Ltd", "test.isam@example.com", date(2026, 3, 1), "CFO")
    conn.execute(
        """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, project, revenue)
           SELECT 'SN248001', register_sync_id, DATE '2025-06-01', 'Test Customer', 'Earlier', 100
             FROM sales.register_sync LIMIT 1"""
    )
    order_json.update(
        order_type="VOLUME", reporting_category="EXPANSION_NN", account_manager_email="test.isam@example.com"
    )
    rules = _rules(conn, order_json)
    assert [r[0] for r in rules] == ["REPORTING_CATEGORY_MISMATCH"]
    assert "expected EXPANSION_E" in rules[0][1]


def test_cannot_allocate_to_a_leaver(conn: Connection, master_data: MasterDataIn) -> None:
    service.employee_leaves(conn, "test.territory@example.com", date(2026, 10, 31))
    with pytest.raises(psycopg.Error, match="unknown or inactive employee"), conn.transaction():
        service.allocate_account(
            conn, "Test Customer Ltd", "test.territory@example.com", date(2027, 1, 1), "CFO"
        )
