"""CFO decision 3 (2026-09-24): only the CFO may approve orders, waive checks, approve losses
and set non-standard credit terms. Checks are always recorded in the actor's own name."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from conftest import CFO, FINANCE, act_as, load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _validated_order(conn: Connection, order_json: dict[str, Any]) -> str:
    number = load(conn, order_json).order_number
    service.change_status(conn, number, "validated", "reviewed")
    act_as(conn, FINANCE)
    for c in service.order_checks(conn, number):
        service.record_check(conn, number, check_type=c["check_type_code"], status="passed", notes="seen")
    return number


def _fails(conn: Connection, fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(psycopg.Error) as info, conn.transaction():
        fn(*args, **kwargs)
    return str(info.value.diag.message_primary)


def test_only_cfo_holds_privileges(conn: Connection) -> None:
    rows = conn.execute(
        """SELECT e.email, array_agg(p.permission_code ORDER BY p.permission_code) AS perms
             FROM sales.employee_permission p JOIN sales.employee e USING (employee_id) GROUP BY e.email"""
    ).fetchall()
    assert rows == [
        {"email": CFO, "perms": ["approve_credit_terms", "approve_loss", "approve_order", "waive_check"]}
    ]


def test_non_cfo_cannot_approve(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _validated_order(conn, order_json)
    msg = _fails(conn, service.change_status, conn, number, "approved", "trying")
    assert msg == f"approving order {number} is not permitted for {FINANCE}"


def test_cfo_can_approve(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    number = _validated_order(conn, order_json)
    act_as(conn, CFO)
    service.change_status(conn, number, "approved", "approved by CFO")
    assert service.order_summary(conn, number)["status_code"] == "approved"


def test_checker_is_always_the_actor(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = _validated_order(conn, order_json)
    checkers = conn.execute(
        """SELECT DISTINCT e.email FROM sales.sales_order_check c
             JOIN sales.employee e ON e.employee_id = c.checked_by_employee_id
             JOIN sales.sales_order o USING (sales_order_id) WHERE o.order_number = %s""",
        (number,),
    ).fetchall()
    assert checkers == [{"email": FINANCE}]


def test_check_cannot_be_recorded_in_someone_elses_name(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    load(conn, order_json)
    act_as(conn, FINANCE)
    conn.execute(
        """UPDATE sales.sales_order_check SET check_status_code = 'passed',
                  checked_by_employee_id = (SELECT employee_id FROM sales.employee WHERE email = %s)
            WHERE check_type_code = 'gdap_relationship'""",
        (CFO,),
    )
    row = conn.execute(
        """SELECT e.email FROM sales.sales_order_check c JOIN sales.employee e
               ON e.employee_id = c.checked_by_employee_id WHERE c.check_type_code = 'gdap_relationship'"""
    ).fetchone()
    assert row == {"email": FINANCE}


def test_unknown_actor_cannot_record_checks(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    act_as(conn, "someone@nowhere.example")
    msg = _fails(conn, service.record_check, conn, number, check_type="gdap_relationship", status="passed")
    assert "identified employee" in msg


def test_checks_must_start_pending(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_id = service.order_summary(conn, load(conn, order_json).order_number)["sales_order_id"]
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.sales_order_check (sales_order_id, check_type_code, check_status_code)
           VALUES (%s, 'margin_approval', 'passed')""",
        (order_id,),
    )
    assert msg == "checks must be created as pending"


def test_only_cfo_can_waive(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    number = load(conn, order_json).order_number
    act_as(conn, FINANCE)
    msg = _fails(
        conn, service.record_check, conn, number, check_type="gdap_relationship", status="waived", notes="n/a"
    )
    assert "waiving check gdap_relationship is not permitted" in msg
    act_as(conn, CFO)
    service.record_check(
        conn, number, check_type="gdap_relationship", status="waived", notes="Tenant is ours"
    )


def test_only_cfo_can_approve_a_loss(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][1]["unit_sell"] = "600.00"
    order_json["lines"][1]["margin_rationale"] = "loss leader"
    order_json.pop("stated_totals")
    number = load(conn, order_json).order_number
    act_as(conn, FINANCE)
    msg = _fails(conn, service.record_check, conn, number, check_type="margin_approval", status="passed")
    assert "approving a loss-making order is not permitted" in msg
    act_as(conn, CFO)
    service.record_check(conn, number, check_type="margin_approval", status="passed", notes="accepted")


def test_only_cfo_sets_non_standard_terms(conn: Connection, master_data: MasterDataIn) -> None:
    statement = """INSERT INTO sales.customer_credit_terms (customer_id, effective_from, effective_to,
                       recurring_terms_days, recurring_payment_method_code, one_off_terms_days,
                       risk_rating_code, is_non_standard, reason)
                   SELECT customer_id, DATE '2025-01-01', DATE '2025-12-31', 60, 'bank_transfer', 60,
                          'standard', true, 'Negotiated at renewal' FROM sales.customer"""
    act_as(conn, FINANCE)
    assert "setting non-standard credit terms is not permitted" in savepoint_rejects(conn, statement)
    act_as(conn, CFO)
    conn.execute(statement)
    row = conn.execute(
        """SELECT e.email FROM sales.customer_credit_terms t JOIN sales.employee e
               ON e.employee_id = t.approved_by_employee_id WHERE t.is_non_standard"""
    ).fetchone()
    assert row == {"email": CFO}


def test_personal_login_required_in_production_mode(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """With sales_orders_person present (production), the shared login can't claim to be the CFO,
    and the CFO's own login is the actor regardless of app.current_user."""
    number = _validated_order(conn, order_json)
    conn.execute("CREATE ROLE sales_orders_person NOLOGIN")
    act_as(conn, CFO)  # spoofing attempt over the shared connection
    msg = _fails(conn, service.change_status, conn, number, "approved", "spoof")
    assert "must be done from a personal login" in msg

    conn.execute(f'CREATE ROLE "{CFO}" LOGIN IN ROLE sales_orders_person')
    conn.execute(f'GRANT USAGE ON SCHEMA sales, audit TO "{CFO}"')
    conn.execute(f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA sales TO "{CFO}"')
    conn.execute(f'SET SESSION AUTHORIZATION "{CFO}"')
    act_as(conn, FINANCE)  # ignored: the personal login wins
    service.change_status(conn, number, "approved", "approved from personal login")
    actor = conn.execute("SELECT audit.current_actor() AS a").fetchone()
    conn.execute("RESET SESSION AUTHORIZATION")
    assert actor == {"a": CFO}
    assert service.order_summary(conn, number)["status_code"] == "approved"
