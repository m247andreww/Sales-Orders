"""Input validation happens before any database write."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import as_new_submission, fixture_json
from pydantic import ValidationError

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.errors import UnknownReferenceError
from sales_orders.models import MasterDataIn, OrderSubmissionIn


def test_reference_fixture_is_valid() -> None:
    OrderSubmissionIn.model_validate(fixture_json("test_order.json"))
    MasterDataIn.model_validate(fixture_json("test_master_data.json"))


def test_float_money_rejected(order_json: dict[str, Any]) -> None:
    order_json["lines"][0]["unit_sell"] = 1195.0
    with pytest.raises(ValidationError, match="not floats"):
        OrderSubmissionIn.model_validate(order_json)


def test_float_fx_rate_rejected() -> None:
    data = fixture_json("test_master_data.json")
    data["fx_rates"][0]["rate"] = 0.7390516
    with pytest.raises(ValidationError, match="not floats"):
        MasterDataIn.model_validate(data)


def test_naive_timestamp_rejected(order_json: dict[str, Any]) -> None:
    order_json["source_email"]["received_at"] = "2026-09-10T15:56:24"
    with pytest.raises(ValidationError, match="timezone"):
        OrderSubmissionIn.model_validate(order_json)


def test_one_off_with_many_periods_rejected(order_json: dict[str, Any]) -> None:
    order_json["lines"][0]["billing_periods"] = 12
    with pytest.raises(ValidationError, match="one_off"):
        OrderSubmissionIn.model_validate(order_json)


def test_unknown_supplier_quote_rejected(order_json: dict[str, Any]) -> None:
    order_json["lines"][3]["supplier_quote_reference"] = "NOPE"
    with pytest.raises(ValidationError, match="not listed in supplier_quotes"):
        OrderSubmissionIn.model_validate(order_json)


def test_fx_rate_required_for_foreign_cost(order_json: dict[str, Any]) -> None:
    del order_json["lines"][3]["fx_rate"]
    with pytest.raises(ValidationError, match="fx_rate is required"):
        OrderSubmissionIn.model_validate(order_json)


def test_order_needs_lines(order_json: dict[str, Any]) -> None:
    order_json["lines"] = []
    with pytest.raises(ValidationError):
        OrderSubmissionIn.model_validate(order_json)


def test_unexpected_fields_rejected(order_json: dict[str, Any]) -> None:
    order_json["discount"] = "10"
    with pytest.raises(ValidationError, match="Extra inputs"):
        OrderSubmissionIn.model_validate(order_json)


@pytest.mark.parametrize(
    ("path", "value", "kind"),
    [
        (("customer_legal_name",), "Test Custmer Ltd", "customer"),
        (("submitted_by_email",), "nobody@example.com", "employee"),
        (("lines", 0, "supplier_name"), "Unknown Supplier", "supplier"),
        (("lines", 3, "fx_rate", "source"), "Bloomberg", "FX rate"),
    ],
)
def test_unknown_master_data_rejected_and_nothing_written(
    conn: Connection,
    master_data: MasterDataIn,
    order_json: dict[str, Any],
    path: tuple[Any, ...],
    value: str,
    kind: str,
) -> None:
    target: Any = order_json
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    before = conn.execute("SELECT count(*) AS n FROM sales.sales_order").fetchone()
    with pytest.raises(UnknownReferenceError) as info, conn.transaction():
        service.create_sales_order(conn, as_new_submission(order_json))
    assert info.value.kind == kind
    assert conn.execute("SELECT count(*) AS n FROM sales.sales_order").fetchone() == before
