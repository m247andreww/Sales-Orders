"""Building account-ownership history from the Register (CFO, 2026-09-24)."""

from __future__ import annotations

from datetime import date
from typing import Any

from conftest import load

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn, RegisterOwnerAliasIn


def _register(conn: Connection, rows: list[tuple[str, str, str, str]]) -> None:
    """rows: (sn, date, salesperson, new_logo) for 'Test Customer'."""
    conn.execute("DELETE FROM sales.register_entry")
    for sn, d, sp, nl in rows:
        conn.execute(
            """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, salesperson, new_logo)
               SELECT %s, register_sync_id, %s::date, 'Test Customer', %s, %s FROM sales.register_sync LIMIT 1""",
            (sn, d, sp, nl),
        )


def _aliases(conn: Connection, master_data: MasterDataIn) -> None:
    service.load_master_data(
        conn,
        MasterDataIn(
            register_owner_aliases=(
                RegisterOwnerAliasIn(
                    register_label="Test Territory Manager", owner_email="test.territory@example.com"
                ),
                RegisterOwnerAliasIn(
                    register_label="Test Internal Sales", owner_email="test.isam@example.com"
                ),
            )
        ),
    )
    conn.execute("DELETE FROM sales.customer_account_allocation")


def _history(conn: Connection) -> list[tuple[str, date, date | None]]:
    rows = conn.execute(
        """SELECT COALESCE(e.email, a.house_account) AS owner, a.allocated_from, a.allocated_to
             FROM sales.customer_account_allocation a LEFT JOIN sales.employee e USING (employee_id)
            ORDER BY a.allocated_from"""
    ).fetchall()
    return [(r["owner"], r["allocated_from"], r["allocated_to"]) for r in rows]


def test_renewals_by_house_do_not_end_salesperson_ownership(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _aliases(conn, master_data)
    _register(
        conn,
        [
            ("SN240001", "2024-01-10", "Auto Renew", "Existing"),
            ("SN250001", "2025-03-01", "Test Territory Manager", "Existing"),
            ("SN250002", "2025-06-01", "Auto Renew", "Existing"),  # central renewal: no change
            ("SN250003", "2025-09-01", "Cust Success", "Existing"),  # no change
            ("SN260001", "2026-02-01", "Test Internal Sales", "Existing"),
        ],
    )
    result = service.build_account_history(conn, "test")
    assert (result["customers_built"], result["periods_created"], result["customers_skipped"]) == (1, 3, 0)
    assert _history(conn) == [
        ("House", date(2024, 1, 10), date(2025, 2, 28)),
        ("test.territory@example.com", date(2025, 3, 1), date(2026, 1, 31)),
        ("test.isam@example.com", date(2026, 2, 1), None),
    ]


def test_existing_marker_sets_existed_before(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    _register(conn, [("SN240001", "2024-01-10", "Test Territory Manager", "Existing")])
    service.build_account_history(conn, "test")
    row = conn.execute("SELECT existed_before, existed_before_source FROM sales.customer").fetchone()
    assert row == {
        "existed_before": date(2024, 1, 10),
        "existed_before_source": "AW SOs Register: first order marked Existing",
    }


def test_pre_register_customer_is_existing_for_first_salesperson(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """First salesperson on a customer that pre-dates the Register: orders are E, not NN."""
    _aliases(conn, master_data)
    _register(conn, [("SN240001", "2024-01-10", "Test Territory Manager", "Existing")])
    service.build_account_history(conn, "test")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_E")
    number = load(conn, order_json).order_number
    assert "REPORTING_CATEGORY_MISMATCH" not in {
        e["rule_code"] for e in service.order_exceptions(conn, number)
    }


def test_new_logo_won_by_salesperson_is_net_new(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _aliases(conn, master_data)
    _register(conn, [("SN250001", "2025-05-01", "Test Territory Manager", "New Logo")])
    service.build_account_history(conn, "test")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    number = load(conn, order_json).order_number
    assert "REPORTING_CATEGORY_MISMATCH" not in {
        e["rule_code"] for e in service.order_exceptions(conn, number)
    }


def test_unmapped_salesperson_label_blocks_that_customer(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    _register(conn, [("SN250001", "2025-05-01", "Someone Unknown", "Existing")])
    result = service.build_account_history(conn, "test")
    assert (result["customers_built"], result["customers_skipped"]) == (0, 1)
    assert [u["salesperson"] for u in result["unmapped_labels"]] == ["Someone Unknown"]
    assert _history(conn) == []


def test_alternating_salespeople_are_reported(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    _register(
        conn,
        [
            ("SN250001", "2025-01-01", "Test Territory Manager", "Existing"),
            ("SN250002", "2025-02-01", "Test Internal Sales", "Existing"),
            ("SN250003", "2025-03-01", "Test Territory Manager", "Existing"),
        ],
    )
    result = service.build_account_history(conn, "test")
    assert [(c["customer"], c["allocated_from"]) for c in result["conflicts"]] == [
        ("Test Customer Ltd", date(2025, 3, 1))
    ]


def test_existing_history_is_never_overwritten(conn: Connection, master_data: MasterDataIn) -> None:
    before = _history(conn)  # fixture allocation (not deleted here)
    _register(conn, [("SN250001", "2025-05-01", "Auto Renew", "Existing")])
    assert service.build_account_history(conn, "test")["customers_built"] == 0
    assert _history(conn) == before


def test_two_salespeople_same_day_is_decided_and_logged(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    _register(
        conn,
        [
            ("SN250001", "2025-01-01", "Test Territory Manager", "Existing"),
            ("SN250002", "2025-05-01", "Test Territory Manager", "Existing"),
            ("SN250003", "2025-05-01", "Test Internal Sales", "Existing"),
        ],
    )
    result = service.build_account_history(conn, "test")
    assert _history(conn) == [
        ("test.territory@example.com", date(2025, 1, 1), date(2025, 4, 30)),
        ("test.isam@example.com", date(2025, 5, 1), None),
    ]
    assert [d["note"] for d in result["decisions"]] == [
        "Two salespeople on 2025-05-01; Test Internal Sales (SN250003) taken as owner"
    ]


def test_same_day_on_first_order(conn: Connection, master_data: MasterDataIn) -> None:
    _aliases(conn, master_data)
    _register(
        conn,
        [
            ("SN250001", "2025-01-01", "Test Territory Manager", "Existing"),
            ("SN250002", "2025-01-01", "Test Internal Sales", "Existing"),
        ],
    )
    result = service.build_account_history(conn, "test")
    assert _history(conn) == [("test.isam@example.com", date(2025, 1, 1), None)]
    assert len(result["decisions"]) == 1
