"""GL coding and the product database."""

from __future__ import annotations

from typing import Any

from conftest import load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _line(conn: Connection, number: str, line_number: int) -> dict[str, Any]:
    return next(ln for ln in service.order_lines(conn, number) if ln["line_number"] == line_number)


def test_missing_gl_is_an_error(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][5].pop("revenue_gl_code")
    order_json["lines"][5].pop("cost_gl_code")
    order_json["lines"][5].pop("service_category")
    rules = {e["rule_code"] for e in service.order_exceptions(conn, load(conn, order_json).order_number)}
    assert rules == {"NO_GL_CODE", "NO_SERVICE_CATEGORY"}


def test_line_gl_overrides_product_default(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][0]["revenue_gl_code"] = "1462"
    number = load(conn, order_json).order_number
    assert (_line(conn, number, 1)["revenue_gl_code"], _line(conn, number, 1)["cost_gl_code"]) == (
        "1462",
        "2400",
    )


def test_revenue_gl_must_be_a_revenue_account(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_line SET revenue_gl_code = '2400'
            WHERE line_number = 1 AND sales_order_id = (SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s)""",
        (number,),
    )
    assert msg == "line 1: revenue GL 2400 is a EXPENSE account, not revenue"


def test_deferred_revenue_is_not_a_line_gl(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_line SET revenue_gl_code = '7217'
            WHERE line_number = 3 AND sales_order_id = (SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s)""",
        (number,),
    )
    assert "is a LIABILITY account" in msg


def test_cost_gl_must_be_direct_cost(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_line SET cost_gl_code = '1301'
            WHERE line_number = 4 AND sales_order_id = (SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s)""",
        (number,),
    )
    assert msg == "line 4: cost GL 1301 is type REVENUE, not a direct cost"


def test_unknown_gl_code_rejected(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    number = load(conn, order_json).order_number
    msg = savepoint_rejects(
        conn,
        """UPDATE sales.sales_order_line SET revenue_gl_code = '1999'
            WHERE line_number = 1 AND sales_order_id = (SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s)""",
        (number,),
    )
    assert "violates foreign key constraint" in msg


def test_product_database_reload_is_idempotent(conn: Connection, master_data: MasterDataIn) -> None:
    before = conn.execute("SELECT count(*) AS n FROM audit.change_log").fetchone()
    service.load_master_data(conn, master_data)
    assert conn.execute("SELECT count(*) AS n FROM audit.change_log").fetchone() == before
