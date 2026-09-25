"""Exception rules: each variant of the clean test order must trip exactly the intended rule."""

from __future__ import annotations

from typing import Any

from conftest import CFO, FINANCE, act_as, load, savepoint_rejects

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import MasterDataIn


def _rules(conn: Connection, order_json: dict[str, Any]) -> set[str]:
    number = load(conn, order_json).order_number
    return {e["rule_code"] for e in service.order_exceptions(conn, number)}


def _checks(conn: Connection, number: str) -> set[str]:
    return {c["check_type_code"] for c in service.order_checks(conn, number)}


def test_tax_pass_through_marked_up(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][5]["unit_sell"] = "50.00"
    order_json.pop("stated_totals")
    assert _rules(conn, order_json) == {"TAX_LINE_MARKED_UP"}


def test_stated_totals_mismatch(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["stated_totals"]["net_sell"] = "15611.99"
    assert _rules(conn, order_json) == {"STATED_TOTAL_MISMATCH"}


def test_stated_totals_within_tolerance(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["stated_totals"]["net_sell"] = "15611.65"  # 4p rounding difference: accepted
    assert _rules(conn, order_json) == set()


def test_loss_line_without_rationale(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][1]["unit_sell"] = "600.00"
    order_json.pop("stated_totals")
    result = load(conn, order_json)
    assert {e["rule_code"] for e in service.order_exceptions(conn, result.order_number)} == {
        "LOSS_LINE_NO_RATIONALE"
    }
    assert "margin_approval" in _checks(conn, result.order_number)


def test_loss_line_with_line_rationale(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"][1]["unit_sell"] = "600.00"
    order_json["lines"][1]["margin_rationale"] = "PM day discounted to win the managed service"
    order_json.pop("stated_totals")
    result = load(conn, order_json)
    assert service.order_exceptions(conn, result.order_number) == []
    assert "margin_approval" in _checks(conn, result.order_number)  # still needs CFO sign-off


def test_order_loss_without_rationale(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"] = [order_json["lines"][2]]
    order_json["lines"][0]["unit_sell"] = "15.00"  # CSP below cost
    order_json["lines"][0]["margin_rationale"] = "line-level note only"
    order_json.pop("stated_totals")
    assert _rules(conn, order_json) == {"ORDER_LOSS_NO_RATIONALE"}


def test_order_rationale_covers_everything(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"] = [order_json["lines"][2]]
    order_json["lines"][0]["unit_sell"] = "15.00"
    order_json["margin_exception_reason"] = "Strategic CSP win approved by CFO"
    order_json.pop("stated_totals")
    assert _rules(conn, order_json) == set()


def test_low_but_positive_margin_is_not_an_exception(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["lines"] = [order_json["lines"][2]]  # CSP alone: 9.14% GM, no minimum any more
    order_json.pop("stated_totals")
    result = load(conn, order_json)
    assert service.order_exceptions(conn, result.order_number) == []
    assert "margin_approval" not in _checks(conn, result.order_number)


def test_high_risk_customer(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    act_as(conn, CFO)  # non-standard terms are CFO-only
    conn.execute(
        """UPDATE sales.customer_credit_terms SET effective_to = DATE '2026-05-31'
            WHERE customer_id = (SELECT customer_id FROM sales.customer)"""
    )
    conn.execute(
        """INSERT INTO sales.customer_credit_terms
               (customer_id, effective_from, recurring_terms_days, recurring_payment_method_code,
                one_off_terms_days, one_off_prepayment_required, risk_rating_code, is_non_standard,
                reason)
           SELECT customer_id, DATE '2026-06-01', 30, 'direct_debit', 0, true, 'high', true,
                  'Customer entered a Company Voluntary Arrangement'
             FROM sales.customer"""
    )
    number = load(conn, order_json).order_number
    exceptions = service.order_exceptions(conn, number)
    assert [e["rule_code"] for e in exceptions] == ["CUSTOMER_CREDIT_RISK"]
    assert "payment on order" in exceptions[0]["message"]


def test_no_credit_terms(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    conn.execute("UPDATE sales.customer_credit_terms SET effective_from = DATE '2027-01-01'")
    assert _rules(conn, order_json) == {"NO_CREDIT_TERMS"}


def test_supplier_not_approved(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE sales.supplier SET account_status_code = 'pending_onboarding' WHERE name = 'Test US Hardware Inc'"
    )
    result = load(conn, order_json)
    assert {e["rule_code"] for e in service.order_exceptions(conn, result.order_number)} == {
        "SUPPLIER_NOT_APPROVED"
    }
    assert "supplier_account_set_up" in _checks(conn, result.order_number)


def test_stale_fx_rate(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    order_json["source_email"]["received_at"] = (
        "2026-10-20T10:00:00+01:00"  # 40 days after the rate; limit 28
    )
    assert _rules(conn, order_json) == {"STALE_FX_RATE"}


def test_missing_signed_order(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["documents"] = [d for d in order_json["documents"] if d["document_type"] != "signed_order"]
    assert _rules(conn, order_json) == {"MISSING_SIGNED_ORDER"}


def test_failed_check(conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]) -> None:
    number = load(conn, order_json).order_number
    act_as(conn, FINANCE)
    service.record_check(
        conn, number, check_type="direct_debit_mandate", status="failed", notes="No mandate on file"
    )
    assert {e["rule_code"] for e in service.order_exceptions(conn, number)} == {"CHECK_FAILED"}


def _register_history(conn: Connection, first_order: str) -> None:
    conn.execute(
        """INSERT INTO sales.register_entry (sn_ref, register_sync_id, date_issued, client, project, revenue)
           SELECT 'SN248001', register_sync_id, %s::date, 'Test Customer', 'Earlier order', 100
             FROM sales.register_sync LIMIT 1""",
        (first_order,),
    )


# Fixture: Test Customer's account was allocated to the territory manager on 2026-01-01.


def test_nn_on_customer_pre_existing_at_allocation(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _register_history(conn, "2024-03-01")  # customer before the salesperson was allocated: existing
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    exceptions = service.order_exceptions(conn, load(conn, order_json).order_number)
    assert [(e["rule_code"], e["severity"]) for e in exceptions] == [
        ("REPORTING_CATEGORY_MISMATCH", "warning")
    ]
    assert (
        "was pre-existing when Test Territory Manager took the account on 2026-01-01: expected EXPANSION_E"
        in exceptions[0]["message"]
    )


def test_e_on_customer_won_by_the_salesperson(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _register_history(conn, "2026-03-01")  # first order after allocation: the salesperson won it
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_E")
    exceptions = service.order_exceptions(conn, load(conn, order_json).order_number)
    assert [e["rule_code"] for e in exceptions] == ["REPORTING_CATEGORY_MISMATCH"]
    assert "expected EXPANSION_NN" in exceptions[0]["message"]


def test_correct_nn_and_e_raise_nothing(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    _register_history(conn, "2026-03-01")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    assert _rules(conn, order_json) == set()


def test_age_of_customer_is_irrelevant(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """Won by the salesperson two years ago is still NN; the old 12-month idea no longer applies."""
    conn.execute("UPDATE sales.customer_account_allocation SET allocated_from = DATE '2024-01-01'")
    _register_history(conn, "2024-06-01")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    assert _rules(conn, order_json) == set()


def test_no_salesperson_and_no_owner_means_nn_e_unverifiable(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    conn.execute("DELETE FROM sales.customer_account_allocation")
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_E", account_manager_email=None)
    assert _rules(conn, order_json) == {"NO_ACCOUNT_ALLOCATION"}


def test_nn_e_judged_for_the_order_salesperson_without_history(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """CFO 2026-09-25: the salesperson named on the order led it, so NN/E is judged for them."""
    conn.execute("DELETE FROM sales.customer_account_allocation")
    _register_history(conn, "2025-01-01")  # customer existed before this salesperson's first order
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    exceptions = service.order_exceptions(conn, load(conn, order_json).order_number)
    assert [e["rule_code"] for e in exceptions] == ["REPORTING_CATEGORY_MISMATCH"]
    assert (
        "Test Territory Manager took the account on 2026-09-09: expected EXPANSION_E"
        in exceptions[0]["message"]
    )


def test_returning_salesperson_keeps_their_original_start(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    """A -> B -> A: A's orders are judged from when A FIRST took the account."""
    conn.execute(
        "UPDATE sales.customer_account_allocation SET allocated_from = DATE '2025-01-01', allocated_to = DATE '2025-06-30'"
    )
    conn.execute(
        """INSERT INTO sales.customer_account_allocation (customer_id, employee_id, allocated_from, allocated_to, source)
           SELECT customer_id, (SELECT employee_id FROM sales.employee WHERE email = 'test.isam@example.com'),
                  DATE '2025-07-01', DATE '2025-12-31', 'test' FROM sales.customer;
           INSERT INTO sales.customer_account_allocation (customer_id, employee_id, allocated_from, source)
           SELECT customer_id, (SELECT employee_id FROM sales.employee WHERE email = 'test.territory@example.com'),
                  DATE '2026-01-01', 'test' FROM sales.customer"""
    )
    _register_history(conn, "2025-02-01")  # first order after A's original start: A won the customer
    order_json.update(order_type="VOLUME", reporting_category="EXPANSION_NN")
    assert _rules(conn, order_json) == set()


def test_salesperson_must_be_account_owner(
    conn: Connection, master_data: MasterDataIn, order_json: dict[str, Any]
) -> None:
    order_json["account_manager_email"] = "test.isam@example.com"
    exceptions = service.order_exceptions(conn, load(conn, order_json).order_number)
    assert [(e["rule_code"], e["severity"]) for e in exceptions] == [
        ("SALESPERSON_NOT_ACCOUNT_OWNER", "warning")
    ]


def test_account_owners_cannot_overlap(conn: Connection, master_data: MasterDataIn) -> None:
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.customer_account_allocation (customer_id, house_account, allocated_from, source)
           SELECT customer_id, 'House', DATE '2026-06-01', 'test' FROM sales.customer""",
    )
    assert "customer_account_allocation_no_overlap" in msg
