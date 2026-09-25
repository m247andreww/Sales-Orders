"""AW SOs Register import and SN sourcing. SNs are never invented: they come from the Register."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import pytest
from conftest import FIXTURES, load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn
from sales_orders.register import RegisterFormatError, canonical_sn, parse_money, parse_register


def _parse() -> Any:
    with (FIXTURES / "test_register.csv").open(encoding="utf-8", newline="") as fh:
        return parse_register(fh)


def test_parser_handles_real_layout() -> None:
    parsed = _parse()
    first = parsed.rows[0]
    assert first.sn_ref == "SN269001"
    assert first.client == "Test Customer"  # first "Client" column
    assert first.signed_by_customer == "Test Signatory"  # second "Client" column
    assert first.date_issued == date(2026, 9, 9)
    assert (first.revenue, first.expected_costs) == (D("15612"), D("10771"))
    assert first.order_category_raw == "New Order"
    assert first.customer_po == "TEST-PO-0001"
    assert first.ticket_ref is None  # "XXX" placeholder


def test_parser_rejects_untrustworthy_rows() -> None:
    parsed = _parse()
    assert [r.sn_ref for r in parsed.rows] == ["SN269001", "SN269001LO", "SN269002"]
    assert [(r.raw_sn, r.reason) for r in parsed.rejections] == [
        ("269002", "duplicate SN reference"),
        ("69003", "not a valid SN reference"),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("260518", "SN260518"),
        ("1808", "SN1808"),
        ("250096Lo", "SN250096LO"),
        ("SN260533", "SN260533"),
        ("260001CA", "SN260001CA"),
        ("60224", None),
        ("250052/53LO", None),
        ("250438C", None),
        ("", None),
    ],
)
def test_canonical_sn(raw: str, expected: str | None) -> None:
    assert canonical_sn(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [(" £ 5,400 ", D("5400")), (" £ (1,234) ", D("-1234")), ("", None)]
)
def test_parse_money(raw: str, expected: D | None) -> None:
    assert parse_money(raw) == expected


def test_layout_change_is_refused() -> None:
    with pytest.raises(RegisterFormatError):
        parse_register(["Date Issued,Client,Project\n", "1 Jan 26,X,Y\n"])


def test_register_mirror_replaced_each_sync(conn: Connection, master_data: MasterDataIn) -> None:
    service.load_register(conn, _parse(), "second sync")
    counts = conn.execute(
        """SELECT (SELECT count(*) FROM sales.register_entry) AS entries,
                  (SELECT count(*) FROM sales.register_sync) AS syncs,
                  (SELECT count(*) FROM sales.register_sync_rejection) AS rejections"""
    ).fetchone()
    assert counts == {"entries": 3, "syncs": 2, "rejections": 4}


def test_last_order_rows_are_never_matched(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    assert {c["sn_ref"] for c in service.sn_candidates(conn, number)} <= {"SN269001"}
    assert service.order_summary(conn, number)  # sanity


def test_no_register_match_leaves_sn_open(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["signed_date"] = "2026-06-01"  # far outside the 7-day window
    number = load(conn, order_json).order_number
    rules = {e["rule_code"] for e in service.order_exceptions(conn, number)}
    assert rules == {"SN_NOT_ASSIGNED"}


def test_ambiguous_match_is_not_guessed(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute(
        """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, project, revenue)
           SELECT 'SN269009', register_sync_id, DATE '2026-09-09', 'Test Customer Limited',
                  'Office & US Store Fit-out', 15612 FROM sales.register_sync LIMIT 1"""
    )
    number = load(conn, order_json).order_number
    assert "SN_NOT_ASSIGNED" in {e["rule_code"] for e in service.order_exceptions(conn, number)}
    assert {c["sn_ref"] for c in service.sn_candidates(conn, number)} == {"SN269001", "SN269009"}
    assert service.assign_sn(conn, number, "SN269009") == "SN269009"  # a person decides


def test_manual_sn_must_exist_for_that_customer(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["signed_date"] = "2026-06-01"
    number = load(conn, order_json).order_number
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order SET sn_ref = 'SN269002', sn_source = 'manual', sn_assigned_at = now()
            WHERE order_number = %s""",
        (number,),
    )
    assert msg == "SN SN269002 is not on the AW SOs Register for this customer"
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order SET sn_ref = 'SN999999', sn_source = 'manual', sn_assigned_at = now()
            WHERE order_number = %s""",
        (number,),
    )
    assert "not on the AW SOs Register" in msg


def test_sn_cannot_be_used_twice(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    load(conn, order_json)  # takes SN269001
    order_json["title"] = "A second order"
    second = load(conn, order_json).order_number
    assert "SN269001" not in {c["sn_ref"] for c in service.sn_candidates(conn, second)}


def test_pending_orders_pick_up_sn_after_sync(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute("DELETE FROM sales.register_entry")
    number = load(conn, order_json).order_number
    assert "SN_NOT_ASSIGNED" in {e["rule_code"] for e in service.order_exceptions(conn, number)}
    service.load_register(conn, _parse(), "later sync")
    assert service.assign_pending_sns(conn) == {number: "SN269001"}
