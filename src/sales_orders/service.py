"""Application services: the only code that writes business data.

All functions take an open connection and run inside the caller's transaction, so a
failure part-way through (e.g. an unknown supplier on line 7) writes nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
from sales_orders.register import ParsedRegister

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


def _order_id(conn: Connection, order_ref: str) -> int:
    """Resolve an order by its internal number (SO-000123) or its SN reference (SN260533)."""
    row = _one(
        conn,
        "SELECT sales_order_id FROM sales.sales_order WHERE order_number = %s OR sn_ref = upper(%s)",
        (order_ref, order_ref),
    )
    if row is None:
        raise OrderNotFoundError(order_ref)
    return int(row["sales_order_id"])


# ============================================================================ master data


def load_master_data(conn: Connection, data: MasterDataIn) -> None:
    """Idempotently insert or update GL accounts, employees, suppliers, customers, products,
    ARR contracts and FX rates (in dependency order)."""
    for g in data.gl_accounts:
        conn.execute(
            """
            INSERT INTO sales.gl_account (account_code, name, account_class, account_type, tax_type, xero_account_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_code) DO UPDATE
               SET name = EXCLUDED.name, account_class = EXCLUDED.account_class,
                   account_type = EXCLUDED.account_type, tax_type = EXCLUDED.tax_type,
                   xero_account_id = COALESCE(EXCLUDED.xero_account_id, sales.gl_account.xero_account_id)
             WHERE (sales.gl_account.name, sales.gl_account.account_class, sales.gl_account.account_type,
                    sales.gl_account.tax_type)
                   IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.account_class, EXCLUDED.account_type, EXCLUDED.tax_type)
                OR (EXCLUDED.xero_account_id IS NOT NULL
                    AND sales.gl_account.xero_account_id IS DISTINCT FROM EXCLUDED.xero_account_id)
            """,
            (g.account_code, g.name, g.account_class, g.account_type, g.tax_type, g.xero_account_id),
        )

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

    for pr in data.products:
        default_supplier = _supplier_id(conn, pr.default_supplier_name) if pr.default_supplier_name else None
        conn.execute(
            """
            INSERT INTO sales.product (sku, name, line_category_code, service_category_code, description,
                                       vendor_part_number, default_supplier_id, default_billing_frequency_code,
                                       default_revenue_gl_code, default_cost_gl_code, list_price, list_cost)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ((upper(sku))) DO UPDATE
               SET name = EXCLUDED.name, line_category_code = EXCLUDED.line_category_code,
                   service_category_code = EXCLUDED.service_category_code,
                   description = EXCLUDED.description, vendor_part_number = EXCLUDED.vendor_part_number,
                   default_supplier_id = EXCLUDED.default_supplier_id,
                   default_billing_frequency_code = EXCLUDED.default_billing_frequency_code,
                   default_revenue_gl_code = EXCLUDED.default_revenue_gl_code,
                   default_cost_gl_code = EXCLUDED.default_cost_gl_code,
                   list_price = EXCLUDED.list_price, list_cost = EXCLUDED.list_cost
             WHERE (sales.product.name, sales.product.line_category_code, sales.product.service_category_code,
                    sales.product.description, sales.product.vendor_part_number, sales.product.default_supplier_id,
                    sales.product.default_billing_frequency_code, sales.product.default_revenue_gl_code,
                    sales.product.default_cost_gl_code, sales.product.list_price, sales.product.list_cost)
                   IS DISTINCT FROM
                   (EXCLUDED.name, EXCLUDED.line_category_code, EXCLUDED.service_category_code,
                    EXCLUDED.description, EXCLUDED.vendor_part_number, EXCLUDED.default_supplier_id,
                    EXCLUDED.default_billing_frequency_code, EXCLUDED.default_revenue_gl_code,
                    EXCLUDED.default_cost_gl_code, EXCLUDED.list_price, EXCLUDED.list_cost)
            """,
            (
                pr.sku,
                pr.name,
                pr.line_category,
                pr.service_category,
                pr.description,
                pr.vendor_part_number,
                default_supplier,
                pr.default_billing_frequency,
                pr.default_revenue_gl_code,
                pr.default_cost_gl_code,
                pr.list_price,
                pr.list_cost,
            ),
        )

    for a in data.arr_contracts:
        customer_id = _customer_id(conn, a.customer_legal_name)
        existing = _one(conn, "SELECT customer_id FROM sales.arr_contract WHERE arr_ref = %s", (a.arr_ref,))
        if existing is not None and int(existing["customer_id"]) != customer_id:
            raise SalesOrderError(f"ARR ref {a.arr_ref} already belongs to another customer")
        conn.execute(
            """
            INSERT INTO sales.arr_contract (arr_ref, customer_id, description, service_category_code,
                                            start_date, end_date, auto_renews, notice_period_days)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (arr_ref) DO UPDATE
               SET description = EXCLUDED.description, service_category_code = EXCLUDED.service_category_code,
                   start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date,
                   auto_renews = EXCLUDED.auto_renews, notice_period_days = EXCLUDED.notice_period_days
             WHERE (sales.arr_contract.description, sales.arr_contract.service_category_code,
                    sales.arr_contract.start_date, sales.arr_contract.end_date,
                    sales.arr_contract.auto_renews, sales.arr_contract.notice_period_days)
                   IS DISTINCT FROM
                   (EXCLUDED.description, EXCLUDED.service_category_code, EXCLUDED.start_date,
                    EXCLUDED.end_date, EXCLUDED.auto_renews, EXCLUDED.notice_period_days)
            """,
            (
                a.arr_ref,
                customer_id,
                a.description,
                a.service_category,
                a.start_date,
                a.end_date,
                a.auto_renews,
                a.notice_period_days,
            ),
        )

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
        INSERT INTO sales.customer (legal_name, trading_name, company_number, xero_contact_id, notes,
                                    xero_tracking_customer, arr_prefix)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ((lower(legal_name))) DO UPDATE
           SET trading_name = EXCLUDED.trading_name,
               company_number = EXCLUDED.company_number,
               xero_contact_id = EXCLUDED.xero_contact_id,
               notes = EXCLUDED.notes,
               xero_tracking_customer = EXCLUDED.xero_tracking_customer,
               arr_prefix = EXCLUDED.arr_prefix
         WHERE (sales.customer.trading_name, sales.customer.company_number, sales.customer.xero_contact_id,
                sales.customer.notes, sales.customer.xero_tracking_customer, sales.customer.arr_prefix)
               IS DISTINCT FROM
               (EXCLUDED.trading_name, EXCLUDED.company_number, EXCLUDED.xero_contact_id, EXCLUDED.notes,
                EXCLUDED.xero_tracking_customer, EXCLUDED.arr_prefix)
        RETURNING customer_id
        """,
        (
            c.legal_name,
            c.trading_name,
            c.company_number,
            c.xero_contact_id,
            c.notes,
            c.xero_tracking_customer,
            c.arr_prefix,
        ),
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
             stated_gross_margin, notes, sn_ref, sn_source, sn_assigned_at, order_document_type_code,
             reporting_category_code, project, ticket_reference, signed_by_customer, signed_by_managed247)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, %s, %s, %s, %s, %s, %s)
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
            sub.sn_ref,
            "Order submission (verified against AW SOs Register)" if sub.sn_ref else None,
            sub.sn_ref,
            sub.order_document_type,
            sub.reporting_category,
            sub.project,
            sub.ticket_reference,
            sub.signed_by_customer,
            sub.signed_by_managed247,
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

    if sub.sn_ref is None:
        conn.execute("SELECT sales.assign_sn_from_register(%s)", (order_id,))

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
    arr_contract = None
    if line.arr_ref:
        arr_contract = _one(
            conn, "SELECT arr_contract_id FROM sales.arr_contract WHERE arr_ref = %s", (line.arr_ref,)
        )
        if arr_contract is None:
            raise UnknownReferenceError("ARR ref", line.arr_ref)

    conn.execute(
        """
        INSERT INTO sales.sales_order_line
            (sales_order_id, line_number, product_id, sku, description, line_category_code, supplier_id,
             supplier_quote_id, quantity, billing_frequency_code, billing_periods, cost_currency_code,
             unit_cost_in_cost_currency, fx_rate_id, unit_sell, margin_rationale, notes,
             service_category_code, revenue_gl_code, cost_gl_code, service_start_date, service_end_date,
             arr_treatment_code, arr_contract_id, supplier_po_number)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s)
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
            line.service_category,
            line.revenue_gl_code,
            line.cost_gl_code,
            line.service_start_date,
            line.service_end_date,
            line.arr_treatment,
            arr_contract["arr_contract_id"] if arr_contract else None,
            line.supplier_po_number,
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
    row = _one(
        conn,
        """SELECT s.*, o.sn_ref, o.reporting_category_code FROM sales.v_sales_order_summary s
             JOIN sales.sales_order o USING (sales_order_id) WHERE s.sales_order_id = %s""",
        (_order_id(conn, order_number),),
    )
    if row is None:
        raise OrderNotFoundError(order_number)
    return row


def order_lines(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    order_id = _order_id(conn, order_number)
    return conn.execute(
        """SELECT l.*, c.arr_ref FROM sales.v_sales_order_line l
             LEFT JOIN sales.arr_contract c USING (arr_contract_id)
            WHERE l.sales_order_id = %s ORDER BY l.line_number""",
        (order_id,),
    ).fetchall()


def order_exceptions(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT rule_code, severity, message FROM sales.v_sales_order_exception_all
            WHERE sales_order_id = %s ORDER BY severity, rule_code, message""",
        (_order_id(conn, order_number),),
    ).fetchall()


def order_checks(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    order_id = _order_id(conn, order_number)
    return conn.execute(
        """SELECT check_type_code, check_status_code, notes FROM sales.sales_order_check
            WHERE sales_order_id = %s ORDER BY check_type_code""",
        (order_id,),
    ).fetchall()


# ============================================================================ SN references


def assign_sn(conn: Connection, order_number: str, sn_ref: str | None = None) -> str | None:
    """Give an order its SN from the AW SOs Register.

    With sn_ref: a person has identified the Register row; the database checks it exists for this
    customer. Without: auto-match, which only assigns when exactly one best candidate exists.
    Returns the SN, or None if it could not be determined (the order keeps SN_NOT_ASSIGNED).
    """
    order_id = _order_id(conn, order_number)
    if sn_ref is None:
        row = _required(
            _one(conn, "SELECT sales.assign_sn_from_register(%s) AS sn", (order_id,)), "assign_sn result"
        )
        return None if row["sn"] is None else str(row["sn"])
    conn.execute(
        """UPDATE sales.sales_order SET sn_ref = %s, sn_source = 'AW SOs Register (manual)', sn_assigned_at = now()
            WHERE sales_order_id = %s""",
        (sn_ref, order_id),
    )
    return sn_ref


def assign_pending_sns(conn: Connection) -> dict[str, str]:
    """After each Register sync: try to source an SN for every order still waiting for one."""
    rows = conn.execute(
        """SELECT order_number, sales.assign_sn_from_register(sales_order_id) AS sn
             FROM sales.sales_order WHERE sn_ref IS NULL AND status_code <> 'cancelled'
            ORDER BY sales_order_id"""
    ).fetchall()
    return {str(r["order_number"]): str(r["sn"]) for r in rows if r["sn"] is not None}


def sn_candidates(conn: Connection, order_number: str) -> list[dict[str, Any]]:
    return conn.execute(
        """SELECT sn_ref, client, project, date_issued, revenue, score FROM sales.v_sn_candidate
            WHERE sales_order_id = %s ORDER BY score DESC, sn_ref""",
        (_order_id(conn, order_number),),
    ).fetchall()


def load_register(conn: Connection, parsed: ParsedRegister, source: str) -> int:
    """Replace the Register mirror with a fresh export, in one transaction. Returns the sync id."""
    sync = _required(
        _one(
            conn,
            """INSERT INTO sales.register_sync (source, rows_loaded, rows_rejected) VALUES (%s, %s, %s)
               RETURNING register_sync_id""",
            (source, len(parsed.rows), len(parsed.rejections)),
        ),
        "register_sync",
    )
    sync_id = int(sync["register_sync_id"])
    conn.execute("DELETE FROM sales.register_entry")
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sales.register_entry
                (sn_ref, register_sync_id, date_issued, document_type_raw, client, new_logo, project,
                 customer_po, ticket_ref, document_date, signed_by_managed247, signed_by_customer,
                 order_category_raw, reporting_category_raw, salesperson, revenue, expected_costs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    r.sn_ref,
                    sync_id,
                    r.date_issued,
                    r.document_type_raw,
                    r.client,
                    r.new_logo,
                    r.project,
                    r.customer_po,
                    r.ticket_ref,
                    r.document_date,
                    r.signed_by_managed247,
                    r.signed_by_customer,
                    r.order_category_raw,
                    r.reporting_category_raw,
                    r.salesperson,
                    r.revenue,
                    r.expected_costs,
                )
                for r in parsed.rows
            ],
        )
        cur.executemany(
            """INSERT INTO sales.register_sync_rejection (register_sync_id, row_number, raw_sn, reason)
               VALUES (%s, %s, %s, %s)""",
            [(sync_id, j.row_number, j.raw_sn, j.reason) for j in parsed.rejections],
        )
    return sync_id


# ============================================================================ ARR


def arr_position(conn: Connection, as_of: date) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM sales.arr_at(%s) ORDER BY arr_ref", (as_of,)).fetchall()


def arr_bridge(conn: Connection, date_from: date, date_to: date) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM sales.arr_bridge(%s, %s) ORDER BY movement_type_code", (date_from, date_to)
    ).fetchall()


_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def money(value: Decimal | None, currency: str = "GBP") -> str:
    """Format for display only; never used in calculations."""
    if value is None:
        return "-"
    symbol = _SYMBOLS.get(currency, currency + " ")
    return f"-{symbol}{-value:,.2f}" if value < 0 else f"{symbol}{value:,.2f}"
