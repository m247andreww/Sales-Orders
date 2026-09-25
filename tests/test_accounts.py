"""House and Legacy accounts, and the leaver routine (CFO, 2026-09-24)."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
import pytest
from conftest import CFO, FINANCE, act_as, load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import EmployeeAbsenceIn, MasterDataIn


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
            "Account owner on 2026-09-09 is House; on approval the account passes to Test Territory Manager, who led this order",
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


def _approve(conn: Connection, number: str) -> None:
    service.change_status(conn, number, "validated", "reviewed")
    act_as(conn, FINANCE)
    for c in service.order_checks(conn, number):
        service.record_check(conn, number, check_type=c["check_type_code"], status="passed", notes="seen")
    act_as(conn, CFO)
    service.change_status(conn, number, "approved", "approved")


def _owners(conn: Connection) -> list[tuple[str, date, date | None]]:
    rows = conn.execute(
        """SELECT COALESCE(e.email, a.house_account) AS owner, a.allocated_from, a.allocated_to
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
            ORDER BY a.allocated_from"""
    ).fetchall()
    return [(r["owner"], r["allocated_from"], r["allocated_to"]) for r in rows]


def test_approval_passes_account_to_order_salesperson(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["account_manager_email"] = "test.isam@example.com"
    _approve(conn, load(conn, order_json).order_number)
    assert _owners(conn) == [
        ("test.territory@example.com", date(2026, 1, 1), date(2026, 9, 8)),
        ("test.isam@example.com", date(2026, 9, 9), None),
    ]


def test_approval_by_existing_owner_changes_nothing(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    before = _owners(conn)
    _approve(conn, load(conn, order_json).order_number)
    assert _owners(conn) == before


def test_house_account_taken_by_salesperson_on_approval(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _make_house(conn, "House")
    _approve(conn, load(conn, order_json).order_number)
    assert _owners(conn) == [
        ("House", date(2026, 1, 1), date(2026, 9, 8)),
        ("test.territory@example.com", date(2026, 9, 9), None),
    ]


def test_backdated_order_does_not_rewrite_later_history(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    service.allocate_account(conn, "Test Customer Ltd", "test.isam@example.com", date(2026, 10, 1), "CFO")
    before = _owners(conn)
    _approve(conn, load(conn, order_json).order_number)  # dated 9 Sep, before the 1 Oct change
    assert _owners(conn) == before
    notes = [r["note"] for r in conn.execute("SELECT note FROM sales.account_history_review").fetchall()]
    assert notes == [
        "SN269001 led by Test Territory Manager is dated before a later ownership change: ownership not changed"
    ]


def test_absence_makes_colleague_order_cover(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """CFO 2026-09-25: a colleague stepping in during the owner's holiday does not take the account."""
    service.record_absence(
        conn,
        EmployeeAbsenceIn(
            email="test.territory@example.com",
            absent_from=date(2026, 9, 1),
            absent_to=date(2026, 9, 14),
            reason="holiday",
            source="test",
        ),
    )
    order_json["account_manager_email"] = "test.isam@example.com"
    number = load(conn, order_json).order_number
    rules = {e["rule_code"]: e["message"] for e in service.order_exceptions(conn, number)}
    assert set(rules) == {"COVER_ORDER"}
    assert "Test Internal Sales led this order covering for Test Territory Manager" in rules["COVER_ORDER"]
    before = _owners(conn)
    _approve(conn, number)
    assert _owners(conn) == before  # account stays with the absent owner


def test_explicit_cover_without_recorded_absence(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["account_manager_email"] = "test.isam@example.com"
    order_json["covering_for_email"] = "test.territory@example.com"
    number = load(conn, order_json).order_number
    assert {e["rule_code"] for e in service.order_exceptions(conn, number)} == {"COVER_ORDER"}
    before = _owners(conn)
    _approve(conn, number)
    assert _owners(conn) == before


def test_cover_order_nn_e_judged_for_absent_owner(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """Owner won the customer (NN); the covering colleague's order is still NN."""
    conn.execute(
        """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, project, revenue)
           SELECT 'SN248001', register_sync_id, DATE '2026-03-01', 'Test Customer', 'Earlier', 100
             FROM sales.register_sync LIMIT 1"""
    )
    service.record_absence(
        conn,
        EmployeeAbsenceIn(
            email="test.territory@example.com",
            absent_from=date(2026, 9, 1),
            absent_to=date(2026, 9, 14),
            reason="holiday",
            source="test",
        ),
    )
    order_json.update(
        account_manager_email="test.isam@example.com", order_type="VOLUME", reporting_category="EXPANSION_NN"
    )
    number = load(conn, order_json).order_number
    assert {e["rule_code"] for e in service.order_exceptions(conn, number)} == {"COVER_ORDER"}


def test_after_absence_ends_colleague_takes_the_account(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    service.record_absence(
        conn,
        EmployeeAbsenceIn(
            email="test.territory@example.com",
            absent_from=date(2026, 8, 1),
            absent_to=date(2026, 8, 14),
            reason="holiday",
            source="test",
        ),
    )
    order_json["account_manager_email"] = "test.isam@example.com"  # order dated 9 Sep: not during absence
    _approve(conn, load(conn, order_json).order_number)
    assert _owners(conn)[-1] == ("test.isam@example.com", date(2026, 9, 9), None)


def test_cannot_cover_for_yourself(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["covering_for_email"] = "test.territory@example.com"  # same as the order salesperson
    with pytest.raises(psycopg.Error, match="sales_order_cover_not_self"), conn.transaction():
        load(conn, order_json)


def test_absences_cannot_overlap(conn: Connection, master_data: MasterDataIn) -> None:
    service.record_absence(
        conn,
        EmployeeAbsenceIn(
            email="test.territory@example.com",
            absent_from=date(2026, 9, 1),
            absent_to=date(2026, 9, 14),
            reason="holiday",
            source="test",
        ),
    )
    with pytest.raises(psycopg.Error, match="employee_absence_no_overlap"), conn.transaction():
        service.record_absence(
            conn,
            EmployeeAbsenceIn(
                email="test.territory@example.com",
                absent_from=date(2026, 9, 10),
                absent_to=date(2026, 9, 20),
                reason="holiday",
                source="test",
            ),
        )
