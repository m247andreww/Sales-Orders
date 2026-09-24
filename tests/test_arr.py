"""ARR database: contracts keyed by ARR ref, an append-only movement ledger posted on approval."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from typing import Any

import psycopg
import pytest
from conftest import CFO, FINANCE, act_as, load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _approve(conn: Connection, number: str) -> None:
    service.change_status(conn, number, "validated", "reviewed")
    act_as(conn, FINANCE)
    for c in service.order_checks(conn, number):
        service.record_check(conn, number, check_type=c["check_type_code"], status="passed", notes="seen")
    act_as(conn, CFO)
    service.change_status(conn, number, "approved", "approved")


def _arr(conn: Connection, as_of: date) -> dict[str, tuple[D, D]]:
    return {r["arr_ref"]: (r["mrr"], r["arr"]) for r in service.arr_position(conn, as_of)}


def test_approval_posts_arr_movements(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _approve(conn, load(conn, order_json).order_number)
    # M365: 25 x 18.60 = 465.00 MRR from 10 Sep 26; internet: 140.33 MRR from 1 Oct 26.
    assert _arr(conn, date(2026, 9, 30)) == {"TST001-26": (D("465.00"), D("5580.00"))}
    assert _arr(conn, date(2026, 10, 1)) == {
        "TST001-26": (D("465.00"), D("5580.00")),
        "TST002-26": (D("140.33"), D("1683.96")),
    }
    bridge = {
        r["movement_type_code"]: r["arr_change"]
        for r in service.arr_bridge(conn, date(2026, 8, 31), date(2026, 10, 31))
    }
    assert bridge == {"new": D("7263.96")}


def test_no_arr_before_approval(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    load(conn, order_json)
    assert _arr(conn, date(2027, 1, 1)) == {}


def test_posting_is_idempotent(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    _approve(conn, number)
    order_id = service.order_summary(conn, number)["sales_order_id"]
    assert conn.execute("SELECT sales.post_arr_movements(%s) AS n", (order_id,)).fetchone() == {"n": 0}


def test_ledger_is_append_only(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _approve(conn, load(conn, order_json).order_number)
    msg = savepoint_rejects(conn, "UPDATE sales.arr_movement SET mrr_delta = 0")
    assert msg == "sales.arr_movement is append-only; post a reversing entry instead"


def test_missing_arr_ref_does_not_block_approval(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """CFO decision 4: the ARR ref follows processing. Warning before, error after, never a block."""
    order_json["lines"][2].pop("arr_ref")
    number = load(conn, order_json).order_number
    assert [(e["rule_code"], e["severity"]) for e in service.order_exceptions(conn, number)] == [
        ("ARR_LINE_NO_ARR_REF", "warning")
    ]
    _approve(conn, number)
    assert [(e["rule_code"], e["severity"]) for e in service.order_exceptions(conn, number)] == [
        ("ARR_LINE_NO_ARR_REF", "error")
    ]
    outstanding = service.arr_refs_outstanding(conn)
    assert [(r["line_number"], r["mrr_not_in_arr"]) for r in outstanding] == [(3, D("465.00"))]
    assert set(_arr(conn, date(2027, 1, 1))) == {"TST002-26"}  # M365 not yet in ARR


def test_linking_arr_ref_after_processing_posts_arr(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][2].pop("arr_ref")
    number = load(conn, order_json).order_number
    _approve(conn, number)
    service.link_arr_ref(conn, "SN269001", 3, "TST001-26")
    assert service.order_exceptions(conn, number) == []
    assert service.arr_refs_outstanding(conn) == []
    assert _arr(conn, date(2026, 10, 1))["TST001-26"] == (D("465.00"), D("5580.00"))


def test_only_the_arr_link_is_allowed_on_a_locked_line(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][2].pop("arr_ref")
    number = load(conn, order_json).order_number
    _approve(conn, number)
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_line
              SET arr_contract_id = (SELECT arr_contract_id FROM sales.arr_contract WHERE arr_ref = 'TST001-26'),
                  unit_sell = 1
            WHERE line_number = 3""",
    )
    assert "lines are locked" in msg


def test_arr_link_must_be_same_customer(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute(
        """INSERT INTO sales.customer (legal_name) VALUES ('Other Test Co');
           INSERT INTO sales.arr_contract (arr_ref, customer_id, description, start_date)
           SELECT 'OTH001-26', customer_id, 'Other', DATE '2026-01-01' FROM sales.customer WHERE legal_name = 'Other Test Co'"""
    )
    order_json["lines"][2].pop("arr_ref")
    number = load(conn, order_json).order_number
    _approve(conn, number)
    with (
        pytest.raises(psycopg.Error, match="ARR contract belongs to a different customer"),
        conn.transaction(),
    ):
        service.link_arr_ref(conn, number, 3, "OTH001-26")


def test_stub_lines_are_not_arr(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][2].pop("arr_ref")
    order_json["lines"][2]["arr_treatment"] = "stub"
    number = load(conn, order_json).order_number
    assert service.order_exceptions(conn, number) == []
    _approve(conn, number)
    assert set(_arr(conn, date(2027, 1, 1))) == {"TST002-26"}


def test_one_off_line_cannot_be_arr(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][0]["arr_treatment"] = "arr"
    with pytest.raises(psycopg.Error, match="a one-off line cannot be ARR"), conn.transaction():
        load(conn, order_json)


def test_churn_posts_negative_arr(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _approve(conn, load(conn, order_json).order_number)
    churn = {
        **order_json,
        "order_type": "CHURN",
        "reporting_category": "CHURN_E",
        "title": "Cancel internet",
        "stated_totals": None,
        "customer_po_reference": None,
        "m365_tenant_guid": None,
        "lines": [
            {**order_json["lines"][4], "service_start_date": "2027-10-01", "service_end_date": "2029-09-30"}
        ],
    }
    conn.execute(
        """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, project, revenue)
           SELECT 'SN269010', register_sync_id, DATE '2026-09-09', 'Test Customer', 'Cancel internet', 0
             FROM sales.register_sync LIMIT 1"""
    )
    act_as(conn, "pytest")
    _approve(conn, load(conn, churn).order_number)
    assert _arr(conn, date(2027, 10, 1))["TST002-26"] == (D("0.00"), D("0.00"))
