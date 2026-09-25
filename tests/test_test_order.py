"""The reference test order: every figure is checked against an independent hand calculation.

Hand calculation (GBP; USD at 0.73905160, unit cost rounded to 4dp, line totals to 2dp):
  1 PS senior consultant  3 x 1  x 700.00 / 1195.00          cost 2,100.00  sell 3,585.00  GM 1,485.00
  2 PS project manager    1 x 1  x 630.00 /  850.00          cost   630.00  sell   850.00  GM   220.00
  3 M365 BP monthly      25 x 12 x  16.90 /   18.60          cost 5,070.00  sell 5,580.00  GM   510.00
  4 Gateway $275         2 x 1  x 203.2392 / 253.89          cost   406.48  sell   507.78  GM   101.30
  5 Internet $95/m       1 x 36 x  70.2099 / 140.33          cost 2,527.56  sell 5,051.88  GM 2,524.32
  6 NY sales tax $50     1 x 1  x  36.9526 / 36.9526         cost    36.95  sell    36.95  GM     0.00
                                                     TOTAL  cost 10,770.99 sell 15,611.61 GM 4,840.62
  MRR sell = 25 x 18.60 + 140.33 = 605.33; MRR cost = 422.50 + 70.21 = 492.71; MRR margin 112.62
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from conftest import fixture_json

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn, OrderSubmissionIn

EXPECTED_LINES = [
    # (unit_cost, net_cost, net_sell, gross_margin)
    (D("700.0000"), D("2100.00"), D("3585.00"), D("1485.00")),
    (D("630.0000"), D("630.00"), D("850.00"), D("220.00")),
    (D("16.9000"), D("5070.00"), D("5580.00"), D("510.00")),
    (D("203.2392"), D("406.48"), D("507.78"), D("101.30")),
    (D("70.2099"), D("2527.56"), D("5051.88"), D("2524.32")),
    (D("36.9526"), D("36.95"), D("36.95"), D("0.00")),
]


def _load_reference_order(conn: Connection) -> service.OrderResult:
    return service.create_sales_order(conn, OrderSubmissionIn.model_validate(fixture_json("test_order.json")))


def test_line_arithmetic_matches_hand_calculation(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    lines = service.order_lines(conn, result.order_number)
    actual = [(ln["unit_cost"], ln["net_cost"], ln["net_sell"], ln["gross_margin"]) for ln in lines]
    assert actual == EXPECTED_LINES


def test_order_totals(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    s = service.order_summary(conn, result.order_number)
    assert result.created
    assert result.order_number.startswith("SO-")
    assert (s["net_cost"], s["net_sell"], s["gross_margin"]) == (D("10770.99"), D("15611.61"), D("4840.62"))
    assert s["gross_margin_pct"] == D("31.01")
    assert s["one_off_sell"] == D("4979.73")
    assert s["recurring_contract_sell"] == D("10631.88")
    assert s["monthly_recurring_sell"] == D("605.33")
    assert s["monthly_recurring_margin"] == D("112.62")
    assert s["status_code"] == "received"


def test_sn_sourced_from_register(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    row = conn.execute(
        "SELECT sn_ref, sn_source FROM sales.sales_order WHERE order_number = %s", (result.order_number,)
    ).fetchone()
    assert row == {"sn_ref": "SN269001", "sn_source": "AW SOs Register (auto-match, score 8)"}


def test_order_can_be_referred_to_by_sn(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    assert service.order_summary(conn, "SN269001")["order_number"] == result.order_number
    assert service.order_summary(conn, "sn269001")["order_number"] == result.order_number


def test_gl_and_category_on_every_line(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    lines = service.order_lines(conn, result.order_number)
    assert [
        (ln["service_category_code"], ln["revenue_gl_code"], ln["cost_gl_code"], ln["arr_treatment_code"])
        for ln in lines
    ] == [
        ("prof_services", "1443", "2400", "none"),  # from product database
        ("prof_services", "1462", "2400", "none"),  # from product database
        ("cloud_services", "1233", "2233", "arr"),  # Microsoft CSP rule (CFQ7...)
        ("hardware", "1301", "2301", "none"),  # from product database
        ("connectivity", "1201", "2201", "arr"),  # stated on the line
        ("other_revenue", "1503", "2325", "none"),  # stated on the line
    ]


def test_fx_rate_is_snapshotted_from_rate_table(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    usd_lines = [
        ln for ln in service.order_lines(conn, result.order_number) if ln["cost_currency_code"] == "USD"
    ]
    assert len(usd_lines) == 3
    assert {ln["fx_rate"] for ln in usd_lines} == {D("0.73905160")}
    assert all(ln["fx_rate_id"] is not None for ln in usd_lines)


def test_reference_order_is_clean(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    assert service.order_exceptions(conn, result.order_number) == []


def test_expected_checks_are_raised(conn: Connection, master_data: MasterDataIn) -> None:
    result = _load_reference_order(conn)
    checks = {
        c["check_type_code"]: c["check_status_code"] for c in service.order_checks(conn, result.order_number)
    }
    assert checks == {
        "signed_order_verified": "pending",
        "credit_terms_confirmed": "pending",
        "customer_po_received": "pending",
        "direct_debit_mandate": "pending",
        "gdap_relationship": "pending",
    }


def test_loading_the_same_email_twice_creates_one_order(conn: Connection, master_data: MasterDataIn) -> None:
    first = _load_reference_order(conn)
    before = _row_counts(conn)
    second = _load_reference_order(conn)
    assert (second.created, second.order_number) == (False, first.order_number)
    assert _row_counts(conn) == before


def test_reloading_master_data_changes_nothing(conn: Connection, master_data: MasterDataIn) -> None:
    before = _row_counts(conn)
    service.load_master_data(conn, master_data)
    assert _row_counts(conn) == before


def test_every_write_is_audited_with_actor(conn: Connection, master_data: MasterDataIn) -> None:
    _load_reference_order(conn)
    rows = conn.execute(
        "SELECT DISTINCT table_name, changed_by FROM audit.change_log WHERE table_name LIKE 'sales.sales_order%'"
    ).fetchall()
    tables = {r["table_name"] for r in rows}
    assert {"sales.sales_order", "sales.sales_order_line", "sales.sales_order_check"} <= tables
    assert {r["changed_by"] for r in rows} == {"pytest"}


def _row_counts(conn: Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT (SELECT count(*) FROM sales.sales_order)       AS orders,
               (SELECT count(*) FROM sales.sales_order_line)  AS lines,
               (SELECT count(*) FROM sales.document)          AS documents,
               (SELECT count(*) FROM sales.customer)          AS customers,
               (SELECT count(*) FROM audit.change_log)        AS audit_rows
        """
    ).fetchone()
    assert row is not None
    return dict(row)
