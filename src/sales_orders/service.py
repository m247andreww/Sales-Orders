"""Application services: the only code that writes business data.

All functions take an open connection and run inside the caller's transaction, so a
failure part-way through (e.g. an unknown supplier on line 7) writes nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sales_orders.checks import required_checks
from sales_orders.db import Connection
from sales_orders.errors import OrderNotFoundError, SalesOrderError, UnknownReferenceError
from sales_orders.models import (
    CustomerIn,
    DocumentIn,
    MasterDataIn,
    OrderLineIn,
    OrderSubmissionIn,
)

# ============================================================================ lookups


def _one(conn: Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    return conn.execute(sql, params).fetchone()


def _required(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    """For rows that must exist (e.g. just inserted). Not an assert: asserts vanish under -O."""
    if row is None:
        raise SalesOrderError(f"internal error: expected {what} row was not found")
    return row


def _employee_id(conn: Connection, email: str) -> int:
    row = _one(conn, "SELECT employee_id FROM sales.employee WHERE lower(email) = lower(%s)", (email,))
    if row is None:
        raise UnknownReferenceError("employee", email)
    return int(row["employee_id"])


def _customer_id(conn: Connection, legal_name: str) -> int:
    row = _one(
        conn, "SELECT customer_id FROM sales.customer WHERE lower(legal_name) = lower(%s)", (legal_name,)
    )
    if row is None:
        raise UnknownReferenceError("customer", legal_name)
    return int(row["customer_id"])


def _supplier_id(conn: Connection, name: str) -> int:
    row = _one(conn, "SELECT supplier_id FROM sales.supplier WHERE lower(name) = lower(%s)", (name,))
    if row is None:
        raise UnknownReferenceError("supplier", name)
    return int(row["supplier_id"])


def _document_id(conn: Connection, sha256: str) -> int:
    row = _one(conn, "SELECT document_id FROM sales.document WHERE sha256 = %s", (sha256,))
    if row is None:
        raise UnknownReferenceError("document", sha256)
    return int(row["document_id"])


def _order_id(conn: Connection, order_number: str) -> int:
    row = _one(conn, "SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s", (order_number,))
    if row is None:
        raise OrderNotFoundError(order_number)
    return int(row["sales_order_id"])


# ============================================================================ master data


def load_master_data(conn: Connection, data: MasterDataIn) -> None:
    """Idempotently insert or update employees, suppliers, customers and FX rates."""
    for e in data.employees:
        conn.execute(
            """
            INSERT INTO sales.employee (email, full_name, job_title) VALUES (%s, %s, %s)
            ON CONFLICT ((lower(email))) DO UPDATE
               SET full_name = EXCLUDED.full_name, job_title = EXCLUDED.job_title
             WHERE (sales.employee.full_name, sales.employee.job_title)
                   IS DISTINCT FROM (EXCLUDED.full_name, EXCLUDED.job_title)
            """,
            (e.email, e.full_name, e.job_title),
        )

    for s in data.suppliers:
        conn.execute(
            """
            INSERT INTO sales.supplier (name, is_internal, account_status_code, payment_terms_days,
                                        default_currency_code, xero_contact_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ((lower(name))) DO UPDATE
               SET is_internal = EXCLUDED.is_internal,
                   account_status_code = EXCLUDED.account_status_code,
                   payment_terms_days = EXCLUDED.payment_terms_days,
                   default_currency_code = EXCLUDED.default_currency_code,
                   xero_contact_id = EXCLUDED.xero_contact_id
             WHERE (sales.supplier.is_internal, sales.supplier.account_status_code,
                    sales.supplier.payment_terms_days, sales.supplier.default_currency_code,
                    sales.supplier.xero_contact_id)
                   IS DISTINCT FROM
                   (EXCLUDED.is_internal, EXCLUDED.account_status_code, EXCLUDED.payment_terms_days,
                    EXCLUDED.default_currency_code, EXCLUDED.xero_contact_id)
            """,
            (
                s.name,
                s.is_internal,
                s.account_status,
                s.payment_terms_days,
                s.default_currency,
                s.xero_contact_id,
            ),
        )

    for c in data.customers:
        _load_customer(conn, c)

    for fx in data.fx_rates:
        conn.execute(
            """
            INSERT INTO sales.fx_rate (from_currency_code, to_currency_code, rate_date, rate, source)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (from_currency_code, to_currency_code, rate_date, source) DO NOTHING
            """,
            (fx.from_currency, fx.to_currency, fx.rate_date, fx.rate, fx.source),
        )
        existing = _one(
            conn,
            """SELECT rate FROM sales.fx_rate WHERE from_currency_code = %s AND to_currency_code = %s
               AND rate_date = %s AND source = %s""",
            (fx.from_currency, fx.to_currency, fx.rate_date, fx.source),
        )
        if existing is not None and existing["rate"] != fx.rate:
            # A published rate for a date/source never changes; a different value is an input error.
            raise SalesOrderError(
                f"FX rate {fx.from_currency}->{fx.to_currency} {fx.rate_date} ({fx.source}) is already "
                f"recorded as {existing['rate']}, not {fx.rate}"
            )


def _load_customer(conn: Connection, c: CustomerIn) -> None:
    row = conn.execute(
        """
        INSERT INTO sales.customer (legal_name, trading_name, company_number, xero_contact_id, notes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT ((lower(legal_name))) DO UPDATE
           SET trading_name = EXCLUDED.trading_name,
               company_number = EXCLUDED.company_number,
               xero_contact_id = EXCLUDED.xero_contact_id,
               notes = EXCLUDED.notes
         WHERE (sales.customer.trading_name, sales.customer.company_number,
                sales.customer.xero_contact_id, sales.customer.notes)
               IS DISTINCT FROM
               (EXCLUDED.trading_name, EXCLUDED.company_number, EXCLUDED.xero_contact_id, EXCLUDED.notes)
        RETURNING customer_id
        """,
        (c.legal_name, c.trading_name, c.company_number, c.xero_contact_id, c.notes),
    ).fetchone()
    customer_id = int(row["customer_id"]) if row else _customer_id(conn, c.legal_name)

    for t in c.credit_terms:
        exists = _one(
            conn,
            "SELECT 1 FROM sales.customer_credit_terms WHERE customer_id = %s AND effective_from = %s",
            (customer_id, t.effective_from),
        )
        if exists:
            continue  # terms are history: change them by closing a period and adding a new one
        approver = _employee_id(conn, t.approved_by_email) if t.approved_by_email else None
        conn.execute(
            """
            INSERT INTO sales.customer_credit_terms
                (customer_id, effective_from, effective_to, recurring_terms_days,
                 recurring_payment_method_code, one_off_terms_days, one_off_prepayment_required,
                 credit_limit, risk_rating_code, is_non_standard, reason,
                 approved_by_employee_id, approved_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                customer_id,
                t.effective_from,
                t.effective_to,
                t.recurring_terms_days,
                t.recurring_payment_method,
                t.one_off_terms_days,
                t.one_off_prepayment_required,
                t.credit_limit,
                t.risk_rating,
                t.is_non_standard,
                t.reason,
                approver,
                t.approved_at,
            ),
        )

    for ct in c.contacts:
        conn.execute(
            """
            INSERT INTO sales.customer_contact (customer_id, email, full_name, role_description)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (customer_id, (lower(email))) DO NOTHING
            """,
            (customer_id, ct.email, ct.full_name, ct.role_description),
        )

    for tn in c.m365_tenants:
        conn.execute(
            """
            INSERT INTO sales.customer_m365_tenant (customer_id, tenant_guid, primary_domain)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_guid) DO NOTHING
            """,
            (customer_id, tn.tenant_guid, tn.primary_domain),
        )
        owner = _required(
            _one(
                conn,
                "SELECT customer_id FROM sales.customer_m365_tenant WHERE tenant_guid = %s",
                (tn.tenant_guid,),
            ),
            "customer_m365_tenant",
        )
        if int(owner["customer_id"]) != customer_id:
            raise SalesOrderError(f"M365 tenant {tn.tenant_guid} already belongs to another customer")


# ============================================================================ orders


@dataclass(frozen=True)
class OrderResult:
    sales_order_id: int
    order_number: str
    created: bool  # False = this email/sequence was already loaded; nothing was written


def create_sales_order(conn: Connection, sub: OrderSubmissionIn) -> OrderResult:
    """Record an order submission. Idempotent on (email Message-ID, source_sequence)."""
    source_email_id = _upsert_source_email(conn, sub)

    existing = _one(
        conn,
        """SELECT sales_order_id, order_number FROM sales.sales_order
            WHERE source_email_id = %s AND source_sequence = %s""",
        (source_email_id, sub.source_sequence),
    )
    if existing is not None:
        return OrderResult(int(existing["sales_order_id"]), str(existing["order_number"]), created=False)

    customer_id = _customer_id(conn, sub.customer_legal_name)
    submitted_by = _employee_id(conn, sub.submitted_by_email)
    account_manager = _employee_id(conn, sub.account_manager_email) if sub.account_manager_email else None
    price_list_id = _price_list_id(conn, sub.price_list_name) if sub.price_list_name else None
    tenant_id = _tenant_id(conn, customer_id, str(sub.m365_tenant_guid)) if sub.m365_tenant_guid else None

    document_ids = [_upsert_document(conn, d, source_email_id) for d in sub.documents]
    quote_ids = _upsert_supplier_quotes(conn, sub)

    stated = sub.stated_totals
    order = conn.execute(
        """
        INSERT INTO sales.sales_order
            (customer_id, title, order_type_code, currency_code, quote_reference, pandadoc_document_id,
             customer_po_reference, price_list_id, customer_m365_tenant_id, signed_date, received_at,
             source_email_id, source_sequence, submitted_by_employee_id, account_manager_employee_id,
             is_expedited, margin_exception_reason, stated_net_cost, stated_net_sell,
             stated_gross_margin, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING sales_order_id, order_number
        """,
        (
            customer_id,
            sub.title,
            sub.order_type,
            sub.currency,
            sub.quote_reference,
            sub.pandadoc_document_id,
            sub.customer_po_reference,
            price_list_id,
            tenant_id,
            sub.signed_date,
            sub.source_email.received_at,
            source_email_id,
            sub.source_sequence,
            submitted_by,
            account_manager,
            sub.is_expedited,
            sub.margin_exception_reason,
            stated.net_cost if stated else None,
            stated.net_sell if stated else None,
            stated.gross_margin if stated else None,
            sub.notes,
        ),
    ).fetchone()
    order = _required(order, "new sales_order")
    order_id = int(order["sales_order_id"])

    for number, line in enumerate(sub.lines, start=1):
        _insert_line(
            conn,
            order_id=order_id,
            number=number,
            line=line,
            order_currency=sub.currency,
            quote_ids=quote_ids,
        )

    for document_id in document_ids:
        conn.execute(
            "INSERT INTO sales.sales_order_document (sales_order_id, document_id) VALUES (%s, %s)",
            (order_id, document_id),
        )

    for check_type, notes in required_checks(conn, order_id, sub):
        conn.execute(
            """
            INSERT INTO sales.sales_order_check (sales_order_id, check_type_code, notes)
            VALUES (%s, %s, %s)
            ON CONFLICT (sales_order_id, check_type_code) DO NOTHING
            """,
            (order_id, check_type, notes),
        )

    return OrderResult(order_id, str(order["order_number"]), created=True)


def _upsert_source_email(conn: Connection, sub: OrderSubmissionIn) -> int:
    e = sub.source_email
    conn.execute(
        """
        INSERT INTO sales.source_email (mailbox, internet_message_id, graph_message_id, conversation_id,
                                        subject, sender_email, received_at, body_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (internet_message_id) DO NOTHING
        """,
        (
            e.mailbox,
            e.internet_message_id,
            e.graph_message_id,
            e.conversation_id,
            e.subject,
            e.sender_email,
            e.received_at,
            e.body_text,
        ),
    )
    row = _one(
        conn,
        "SELECT source_email_id FROM sales.source_email WHERE internet_message_id = %s",
        (e.internet_message_id,),
    )
    return int(_required(row, "source_email")["source_email_id"])


def _upsert_document(conn: Connection, d: DocumentIn, source_email_id: int) -> int:
    conn.execute(
        """
        INSERT INTO sales.document (document_type_code, file_name, content_type, size_bytes, sha256,
                                    storage_uri, source_email_id, pandadoc_document_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (sha256) DO NOTHING
        """,
        (
            d.document_type,
            d.file_name,
            d.content_type,
            d.size_bytes,
            d.sha256,
            d.storage_uri,
            source_email_id,
            d.pandadoc_document_id,
        ),
    )
    return _document_id(conn, d.sha256)


def _upsert_supplier_quotes(conn: Connection, sub: OrderSubmissionIn) -> dict[tuple[int, str], int]:
    ids: dict[tuple[int, str], int] = {}
    for q in sub.supplier_quotes:
        supplier_id = _supplier_id(conn, q.supplier_name)
        document_id = _document_id(conn, q.document_sha256) if q.document_sha256 else None
        conn.execute(
            """
            INSERT INTO sales.supplier_quote (supplier_id, supplier_reference, quote_date, valid_until,
                                              currency_code, document_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (supplier_id, supplier_reference) DO NOTHING
            """,
            (supplier_id, q.supplier_reference, q.quote_date, q.valid_until, q.currency, document_id),
        )
        row = _one(
            conn,
            """SELECT supplier_quote_id FROM sales.supplier_quote
                WHERE supplier_id = %s AND supplier_reference = %s""",
            (supplier_id, q.supplier_reference),
        )
        ids[(supplier_id, q.supplier_reference)] = int(_required(row, "supplier_quote")["supplier_quote_id"])
    return ids


def _insert_line(
    conn: Connection,
    *,
    order_id: int,
    number: int,
    line: OrderLineIn,
    order_currency: str,
    quote_ids: dict[tuple[int, str], int],
) -> None:
    supplier_id = _supplier_id(conn, line.supplier_name)
    quote_id = (
        quote_ids[(supplier_id, line.supplier_quote_reference)] if line.supplier_quote_reference else None
    )

    fx_rate_id = None
    if line.fx_rate is not None:
        fx = _one(
            conn,
            """SELECT fx_rate_id FROM sales.fx_rate
                WHERE from_currency_code = %s AND to_currency_code = %s AND rate_date = %s AND source = %s""",
            (line.cost_currency, order_currency, line.fx_rate.rate_date, line.fx_rate.source),
        )
        if fx is None:
            raise UnknownReferenceError(
                "FX rate",
                f"{line.cost_currency}->{order_currency} {line.fx_rate.rate_date} {line.fx_rate.source}",
            )
        fx_rate_id = int(fx["fx_rate_id"])

    product = (
        _one(conn, "SELECT product_id FROM sales.product WHERE upper(sku) = upper(%s)", (line.sku,))
        if line.sku
        else None
    )

    conn.execute(
        """
        INSERT INTO sales.sales_order_line
            (sales_order_id, line_number, product_id, sku, description, line_category_code, supplier_id,
             supplier_quote_id, quantity, billing_frequency_code, billing_periods, cost_currency_code,
             unit_cost_in_cost_currency, fx_rate_id, unit_sell, margin_rationale, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            order_id,
            number,
            product["product_id"] if product else None,
            line.sku,
            line.description,
            line.line_category,
            supplier_id,
            quote_id,
            line.quantity,
            line.billing_frequency,
            line.billing_periods,
            line.cost_currency,
            line.unit_cost,
            fx_rate_id,
            line.unit_sell,
            line.margin_rationale,
            line.notes,
        ),
    )


def _price_list_id(conn: Connection, name: str) -> int:
    row = _one(conn, "SELECT price_list_id FROM sales.price_list WHERE lower(name) = lower(%s)", (name,))
    if row is None:
        raise UnknownReferenceError("price list", name)
    return int(row["price_list_id"])


def _tenant_id(conn: Connection, customer_id: int, tenant_guid: str) -> int:
    row = _one(
        conn,
        """SELECT customer_m365_tenant_id FROM sales.customer_m365_tenant
            WHERE customer_id = %s AND tenant_guid = %s""",
        (customer_id, tenant_guid),
    )
    if row is None:
        raise UnknownReferenceError("M365 tenant for this customer", tenant_guid)
    return int(row["customer_m365_tenant_id"])


# ============================================================================ workflow


def change_status(conn: Connection, order_number: str, to_status: str, reason: str) -> None:
    """Move an order through the workflow. The database enforces allowed transitions and gates."""
    if not reason.strip():
        raise ValueError("a reason is required for every status change")
    order_id = _order_id(conn, order_number)
    conn.execute("SELECT set_config('app.status_reason', %s, true)", (reason,))
    conn.execute(
        "UPDATE sales.sales_order SET status_code = %s WHERE sales_order_id = %s", (to_status, order_id)
    )
    conn.execute("SELECT set_config('app.status_reason', '', true)")


def record_check(
    conn: Connection,
    order_number: str,
    *,
    check_type: str,
    status: str,
    notes: str | None = None,
    evidence_sha256: str | None = None,
) -> None:
    """Record a check outcome. The checker is always the acting employee (set by the database)."""
    order_id = _order_id(conn, order_number)
    evidence = _document_id(conn, evidence_sha256) if evidence_sha256 else None
    updated = conn.execute(
        """
        UPDATE sales.sales_order_check
           SET check_status_code = %s,
               notes = COALESCE(%s, notes),
               evidence_document_id = COALESCE(%s, evidence_document_id)
         WHERE sales_order_id = %s AND check_type_code = %s
        """,
        (status, notes, evidence, order_id, check_type),
    )
    if updated.rowcount != 1:
        raise SalesOrderError(f"order {order_number} has no {check_type!r} check")


# ============================================================================ queries


def order_summary(conn: Connection, order_number: str) -> dict[str, Any]:
    row = _one(conn, "SELECT * FROM sales.v_sales_order_summary WHERE order_number = %s", (order_number,))
    if row is None:
        raise OrderNotFoundError(order_number)
    return row


def order_lines(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    order_id = _order_id(conn, order_number)
    return conn.execute(
        "SELECT * FROM sales.v_sales_order_line WHERE sales_order_id = %s ORDER BY line_number", (order_id,)
    ).fetchall()


def order_exceptions(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT rule_code, severity, message FROM sales.v_sales_order_exception
            WHERE order_number = %s ORDER BY severity, rule_code, message""",
        (order_number,),
    ).fetchall()


def order_checks(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    order_id = _order_id(conn, order_number)
    return conn.execute(
        """SELECT check_type_code, check_status_code, notes FROM sales.sales_order_check
            WHERE sales_order_id = %s ORDER BY check_type_code""",
        (order_id,),
    ).fetchall()


_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def money(value: Decimal | None, currency: str = "GBP") -> str:
    """Format for display only; never used in calculations."""
    if value is None:
        return "-"
    symbol = _SYMBOLS.get(currency, currency + " ")
    return f"-{symbol}{-value:,.2f}" if value < 0 else f"{symbol}{value:,.2f}"
