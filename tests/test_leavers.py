"""Leaving dates (CFO, 2026-09-25): a salesperson leaves on their last working day (gardening leave
counts as left). Orders credited to them afterwards keep the credit but never give them an account."""

from __future__ import annotations

from datetime import date

import psycopg
import pytest
from conftest import savepoint_rejects
from test_account_history import _aliases, _history, _register

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn

TERRITORY = "test.territory@example.com"
ISAM = "test.isam@example.com"


def _periods(conn: Connection, periods: list[tuple[str, str, str | None]]) -> None:
    """Replace Test Customer's ownership with (owner email, from, to) periods."""
    conn.execute("DELETE FROM sales.customer_account_allocation")
    for email, start, end in periods:
        conn.execute(
            """INSERT INTO sales.customer_account_allocation (customer_id, employee_id, allocated_from, allocated_to, source)
               SELECT c.customer_id, e.employee_id, %s::date, %s::date, 'test'
                 FROM sales.customer c, sales.employee e WHERE e.email = %s""",
            (start, end, email),
        )


def _reviews(conn: Connection) -> list[str]:
    return [r["note"] for r in conn.execute("SELECT note FROM sales.account_history_review ORDER BY on_date")]


def test_leaving_records_the_last_working_day(conn: Connection, master_data: MasterDataIn) -> None:
    service.employee_leaves(conn, TERRITORY, date(2025, 12, 14))
    row = conn.execute(
        "SELECT is_active, left_on FROM sales.employee WHERE email = %s", (TERRITORY,)
    ).fetchone()
    assert row == {"is_active": False, "left_on": date(2025, 12, 14)}


def test_leaving_twice_with_a_different_date_is_refused(conn: Connection, master_data: MasterDataIn) -> None:
    service.employee_leaves(conn, TERRITORY, date(2025, 12, 14))
    assert service.employee_leaves(conn, TERRITORY, date(2025, 12, 14)) == 0  # same date: nothing to do
    with pytest.raises(psycopg.errors.CheckViolation, match="already recorded as leaving on 2025-12-14"):
        service.employee_leaves(conn, TERRITORY, date(2026, 3, 31))


def test_an_active_employee_cannot_have_a_leaving_date(conn: Connection, master_data: MasterDataIn) -> None:
    message = savepoint_rejects(
        conn, "UPDATE sales.employee SET left_on = '2025-12-14' WHERE email = %s", (TERRITORY,)
    )
    assert "employee_left_is_inactive" in message


def test_house_period_ends_where_the_leavers_period_would_have(
    conn: Connection, master_data: MasterDataIn
) -> None:
    # regression: the House period used to be open-ended and collided with the later owner
    _periods(conn, [(TERRITORY, "2026-01-01", "2026-06-30"), (ISAM, "2026-07-01", None)])
    assert service.employee_leaves(conn, TERRITORY, date(2026, 3, 31)) == 1
    assert _history(conn) == [
        (TERRITORY, date(2026, 1, 1), date(2026, 3, 31)),
        ("House", date(2026, 4, 1), date(2026, 6, 30)),
        (ISAM, date(2026, 7, 1), None),
    ]


def test_order_credited_after_leaving_leaves_the_account_with_the_previous_owner(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _periods(
        conn,
        [
            (TERRITORY, "2026-01-01", "2026-05-31"),
            (ISAM, "2026-06-01", "2026-07-31"),
            (TERRITORY, "2026-08-01", None),
        ],
    )
    service.employee_leaves(conn, TERRITORY, date(2026, 3, 31))
    assert _history(conn) == [
        (TERRITORY, date(2026, 1, 1), date(2026, 3, 31)),
        ("House", date(2026, 4, 1), date(2026, 5, 31)),
        (ISAM, date(2026, 6, 1), date(2026, 7, 31)),
        (ISAM, date(2026, 8, 1), None),  # not handed back to the leaver
    ]
    assert _reviews(conn) == [
        "Order credited to Test Territory Manager after their last day (2026-03-31): ownership not passed to them"
    ]


def test_history_build_ignores_credit_after_leaving_for_ownership(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _aliases(conn, master_data)
    service.employee_leaves(conn, TERRITORY, date(2025, 12, 14))
    _register(
        conn,
        [
            ("SN250001", "2025-01-10", "Test Territory Manager", "Existing"),
            ("SN250002", "2025-10-26", "Test Internal Sales", "Existing"),
            ("SN250003", "2025-12-26", "Test Territory Manager", "Existing"),  # after the last day
        ],
    )
    service.build_account_history(conn, "test")
    assert _history(conn) == [
        (TERRITORY, date(2025, 1, 10), date(2025, 10, 25)),
        (ISAM, date(2025, 10, 26), None),
    ]
    flagged = conn.execute("SELECT sn_ref, left_on FROM sales.v_register_credit_after_leaving").fetchall()
    assert flagged == [{"sn_ref": "SN250003", "left_on": date(2025, 12, 14)}]


def test_history_build_moves_a_leavers_accounts_to_house(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    service.employee_leaves(conn, TERRITORY, date(2025, 12, 14))
    _register(
        conn,
        [
            ("SN250001", "2025-01-10", "Test Territory Manager", "Existing"),
            ("SN250003", "2025-12-26", "Test Territory Manager", "Existing"),
        ],
    )
    service.build_account_history(conn, "test")
    assert _history(conn) == [
        (TERRITORY, date(2025, 1, 10), date(2025, 12, 14)),
        ("House", date(2025, 12, 15), None),
    ]
