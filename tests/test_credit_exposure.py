"""Credit exposure: invoices owed + in-progress PandaDoc against the credit limit (migration 0021).

All companies, names and figures are synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from conftest import CFO, act_as

from sales_orders import credit, credit_exposure
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data
from sales_orders.xero import XeroDirectoryContact

ACME = UUID("00000000-0000-4000-8000-0000000f0001")
BETA = UUID("00000000-0000-4000-8000-0000000f0002")


def _setup(conn: Connection) -> None:
    credit.sync_xero_contacts(
        conn,
        [
            XeroDirectoryContact(ACME, "Acme Widgets Ltd", "01234567", True, False, "ACTIVE"),
            XeroDirectoryContact(BETA, "Beta Services Ltd", None, True, False, "ACTIVE"),
        ],
    )
    load_master_data(
        conn,
        MasterDataIn(
            customers=(CustomerIn(legal_name="Acme Widgets", arr_prefix="ACM", xero_contact_id=ACME),),
            credit_subjects=(
                CreditSubjectIn(
                    display_name="Acme Widgets",
                    relationship="customer",
                    customer_legal_name="Acme Widgets",
                    company_number="01234567",
                ),
            ),
        ),
    )
    row = conn.execute("SELECT customer_id FROM sales.customer WHERE legal_name = 'Acme Widgets'").fetchone()
    assert row is not None
    act_as(conn, CFO)
    conn.execute(
        "SELECT sales.set_credit_limit(%s, 10000, 'cfo_decision', NULL, 'test limit', NULL)",
        (row["customer_id"],),
    )


def _receivables(conn: Connection, *rows: tuple[str, str, str]) -> list[str]:
    _, unmatched = credit_exposure.load_receivables(
        conn,
        {
            "as_of": "2026-10-02",
            "source": f"test {rows!r}",
            "contacts": [
                {
                    "xero_contact_id": None,
                    "name": n,
                    "current": cur,
                    "overdue": od,
                    "overdue_over_60": "0.00",
                    "oldest_due_date": None,
                    "invoice_count": 1,
                }
                for n, cur, od in rows
            ],
        },
    )
    return unmatched


def _pipeline(conn: Connection, *docs: tuple[str, str | None, str]) -> list[str]:
    _, unmatched = credit_exposure.load_pipeline(
        conn,
        {
            "as_of": "2026-10-02",
            "source": f"test {docs!r}",
            "documents": [
                {
                    "id": f"doc{i}",
                    "name": name,
                    "status": "Sent",
                    "grand_total": total,
                    "currency": "GBP",
                    "client_company": company,
                }
                for i, (name, company, total) in enumerate(docs)
            ],
        },
    )
    return unmatched


def _row(conn: Connection, contact: UUID) -> dict[str, object]:
    return next(r for r in credit_exposure.exposure_rows(conn) if r["xero_contact_id"] == str(contact))


def test_owed_plus_pipeline_with_vat_is_compared_to_the_limit(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn)
    assert _receivables(
        conn, ("Acme Widgets Ltd", "3000.00", "2500.00"), ("Nobody Known", "1.00", "0.00")
    ) == ["Nobody Known"]
    assert _pipeline(conn, ("Acme - Laptop refresh", None, "4000.00")) == []
    r = _row(conn, ACME)
    assert (r["outstanding"], r["pipeline_largest"], r["pipeline_gross"]) == (
        Decimal(5500),
        Decimal(4000),
        Decimal(4800),
    )
    assert (r["exposure"], r["headroom"]) == (Decimal(5500), Decimal(4500))  # what is owed, against the limit
    assert (r["exposure_if_signed"], r["headroom_if_signed"], r["status"]) == (
        Decimal(10300),
        Decimal(-300),
        "over_if_signed",
    )


def test_alternative_quotes_are_not_added_together(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    _pipeline(conn, ("Acme - Option 1", None, "3000.00"), ("Acme - Option 2", None, "5000.00"))
    r = _row(conn, ACME)
    assert (r["pipeline_count"], r["pipeline_value"], r["pipeline_largest"]) == (
        2,
        Decimal(8000),
        Decimal(5000),
    )
    assert r["exposure_if_signed"] == Decimal(6000)  # the largest one, plus VAT; never the sum


def test_customer_without_a_limit_is_reported(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    _receivables(conn, ("Beta Services Ltd", "0.00", "900.00"))
    _pipeline(conn, ("Proposal for growth", "Beta Services", "100.00"))  # matched by client company
    r = _row(conn, BETA)
    assert (r["credit_limit"], r["exposure"], r["exposure_if_signed"], r["status"]) == (
        None,
        Decimal(900),
        Decimal(1020),
        "no_limit",
    )


def test_amounts_must_not_be_floats(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    with pytest.raises(SalesOrderError, match="not floats"):
        credit_exposure.load_receivables(
            conn,
            {
                "as_of": "2026-10-02",
                "source": "float test",
                "contacts": [
                    {"name": "Acme Widgets Ltd", "current": 1.5, "overdue": "0", "invoice_count": 1}
                ],
            },
        )


def test_the_same_file_cannot_be_loaded_twice(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    _pipeline(conn, ("Acme - Laptop refresh", None, "4000.00"))
    with pytest.raises(SalesOrderError, match="already loaded"):
        _pipeline(conn, ("Acme - Laptop refresh", None, "4000.00"))


def test_unmatched_document_is_listed_not_guessed(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    assert _pipeline(conn, ("Mutual Non-Disclosure Agreement", None, "250.00")) == [
        "Mutual Non-Disclosure Agreement"
    ]
    assert [d["name"] for d in credit_exposure.latest_unmatched(conn)["pipeline"]] == [
        "Mutual Non-Disclosure Agreement"
    ]
    assert _row(conn, ACME)["pipeline_value"] == Decimal(0)
