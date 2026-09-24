"""Which pre-processing checks an order needs.

Each rule is a small function returning (check_type, reason) or None. To add a rule,
write a function and add it to RULES. Checks start 'pending'; an order cannot be
approved until every check is passed, waived (with a note) or not applicable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

from sales_orders.db import Connection
from sales_orders.models import OrderSubmissionIn

CheckRule = Callable[[Connection, int, OrderSubmissionIn], tuple[str, str] | None]

# Microsoft CSP (NCE) SKUs start with this prefix, e.g. CFQ7TTC0LFLX:0001.
MICROSOFT_CSP_SKU_PREFIX = "CFQ7"


def _signed_order(_c: Connection, _o: int, _s: OrderSubmissionIn) -> tuple[str, str]:
    return ("signed_order_verified", "Always required: signed document matches submitted pricing")


def _credit_terms(_c: Connection, _o: int, _s: OrderSubmissionIn) -> tuple[str, str]:
    return ("credit_terms_confirmed", "Always required: order terms match the credit terms register")


def _customer_po(_c: Connection, _o: int, s: OrderSubmissionIn) -> tuple[str, str] | None:
    if s.customer_po_reference:
        return ("customer_po_received", f"Customer PO {s.customer_po_reference} quoted on submission")
    return None


def _gdap(_c: Connection, _o: int, s: OrderSubmissionIn) -> tuple[str, str] | None:
    has_csp = any((ln.sku or "").upper().startswith(MICROSOFT_CSP_SKU_PREFIX) for ln in s.lines)
    if has_csp or s.m365_tenant_guid is not None:
        return ("gdap_relationship", "Order contains Microsoft CSP licences")
    return None


def _direct_debit(conn: Connection, order_id: int, _s: OrderSubmissionIn) -> tuple[str, str] | None:
    row = conn.execute(
        """
        SELECT 1
          FROM sales.sales_order o
          JOIN sales.customer_credit_terms t
            ON t.customer_id = o.customer_id
           AND t.effective_from <= o.received_at::date
           AND (t.effective_to IS NULL OR t.effective_to >= o.received_at::date)
         WHERE o.sales_order_id = %s
           AND t.recurring_payment_method_code = 'direct_debit'
           AND EXISTS (SELECT 1 FROM sales.v_sales_order_line l
                        WHERE l.sales_order_id = o.sales_order_id AND l.is_recurring)
        """,
        (order_id,),
    ).fetchone()
    if row:
        return ("direct_debit_mandate", "Recurring charges and customer terms require Direct Debit")
    return None


def _supplier_account(conn: Connection, order_id: int, _s: OrderSubmissionIn) -> tuple[str, str] | None:
    rows = conn.execute(
        """
        SELECT DISTINCT sp.name
          FROM sales.sales_order_line l
          JOIN sales.supplier sp USING (supplier_id)
          JOIN sales.supplier_account_status sas ON sas.status_code = sp.account_status_code
         WHERE l.sales_order_id = %s AND NOT sas.can_order
         ORDER BY sp.name
        """,
        (order_id,),
    ).fetchall()
    if rows:
        return ("supplier_account_set_up", "No approved account: " + ", ".join(r["name"] for r in rows))
    return None


def _loss(conn: Connection, order_id: int, _s: OrderSubmissionIn) -> tuple[str, str] | None:
    """No minimum margin (CFO, 2026-09-24), but any sale below cost needs CFO approval."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE l.gross_margin < 0) AS loss_lines,
               COALESCE(sum(l.gross_margin), 0)           AS order_margin
          FROM sales.sales_order_line l
         WHERE l.sales_order_id = %s
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    loss_lines: int = row["loss_lines"]
    order_margin: Decimal = row["order_margin"]
    if loss_lines or order_margin < 0:
        return (
            "margin_approval",
            f"{loss_lines} line(s) below cost; order margin {order_margin}. CFO approval required",
        )
    return None


RULES: tuple[CheckRule, ...] = (
    _signed_order,
    _credit_terms,
    _customer_po,
    _gdap,
    _direct_debit,
    _supplier_account,
    _loss,
)


def required_checks(
    conn: Connection, order_id: int, submission: OrderSubmissionIn
) -> Iterator[tuple[str, str | None]]:
    """Checks produced by the rules, plus any the submitter asked for explicitly."""
    for rule in RULES:
        result = rule(conn, order_id, submission)
        if result is not None:
            yield result
    for check in submission.checks:
        yield (check.check_type, check.notes)
