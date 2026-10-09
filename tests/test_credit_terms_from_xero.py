"""Payment terms come from Xero (migration 0027). All companies, names and figures are synthetic."""

from __future__ import annotations

from uuid import UUID

import pytest
from conftest import CFO, act_as

from sales_orders import credit, xero
from sales_orders.credit import PaymentTerms
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data
from sales_orders.xero import XeroDirectoryContact

ACME = UUID("00000000-0000-4000-8000-0000000f2001")
BETA = UUID("00000000-0000-4000-8000-0000000f2002")


def _setup(conn: Connection, acme_terms: tuple[int | None, str | None] = (None, None)) -> None:
    credit.sync_xero_contacts(
        conn,
        [
            XeroDirectoryContact(ACME, "Acme Widgets Ltd", "01234567", True, False, "ACTIVE", *acme_terms),
            XeroDirectoryContact(BETA, "Beta Services Ltd", "07654321", True, False, "ACTIVE"),
        ],
    )
    load_master_data(
        conn,
        MasterDataIn(
            customers=(
                CustomerIn(legal_name="Acme Widgets", arr_prefix="ACM", xero_contact_id=ACME),
                CustomerIn(legal_name="Beta Services", arr_prefix="BET", xero_contact_id=BETA),
            ),
            credit_subjects=tuple(
                CreditSubjectIn(
                    display_name=n, relationship="customer", customer_legal_name=n, company_number=k
                )
                for n, k in (("Acme Widgets", "01234567"), ("Beta Services", "07654321"))
            ),
        ),
    )
    act_as(conn, CFO)


def _invoices(conn: Connection, *rows: tuple[str, UUID, str, str]) -> list[str]:
    _, unmatched = credit.load_invoices(
        conn,
        {
            "as_of": "2026-10-09",
            "source": f"test {rows!r}",
            "invoices": [
                {"invoice_number": n, "xero_contact_id": str(c), "invoice_date": d, "due_date": due}
                for n, c, d, due in rows
            ],
        },
    )
    return unmatched


def _terms(conn: Connection, name: str) -> tuple[str, str, str | None]:
    row = conn.execute("SELECT customer_id FROM sales.customer WHERE legal_name = %s", (name,)).fetchone()
    assert row is not None
    t = credit.customer_terms(conn, int(row["customer_id"]))
    return t["recurring"], t["one_off"], t["source"]


def test_parse_reads_the_contacts_sales_terms() -> None:
    contacts = xero.parse_contacts(
        {
            "Contacts": [
                {
                    "ContactID": str(ACME),
                    "Name": "Acme",
                    "PaymentTerms": {"Sales": {"Day": 60, "Type": "DAYSAFTERBILLDATE"}},
                },
                {"ContactID": str(BETA), "Name": "Beta"},
            ]
        }
    )
    assert [(c.sales_terms_days, c.sales_terms_type) for c in contacts] == [
        (60, "DAYSAFTERBILLDATE"),
        (None, None),
    ]


def test_parse_refuses_unknown_terms() -> None:
    with pytest.raises(xero.XeroFormatError):
        xero.parse_contacts(
            {
                "Contacts": [
                    {
                        "ContactID": str(ACME),
                        "Name": "A",
                        "PaymentTerms": {"Sales": {"Day": 5, "Type": "SOON"}},
                    }
                ]
            }
        )


def test_rd_invoices_mean_direct_debit_and_contact_terms_set_one_off(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _setup(conn, (60, "DAYSAFTERBILLDATE"))
    _invoices(
        conn,
        ("RD-1", ACME, "2026-09-26", "2026-10-26"),
        ("RD-2", ACME, "2026-09-26", "2026-10-26"),
        ("RI-3", BETA, "2026-09-26", "2026-11-25"),
    )
    changes = credit.sync_terms_from_xero(conn)
    assert {c["customer"] for c in changes} == {"Acme Widgets", "Beta Services"}
    assert _terms(conn, "Acme Widgets") == ("30 days (Direct Debit)", "60 days", "xero")
    assert _terms(conn, "Beta Services") == ("60 days (bank transfer)", "30 days", "xero")
    assert credit.sync_terms_from_xero(conn) == []  # nothing changes on a second run


def test_a_cfo_change_not_yet_in_xero_is_not_overwritten(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn, (60, "DAYSAFTERBILLDATE"))
    credit.set_payment_terms(conn, "Acme Widgets", PaymentTerms(30, "bank_transfer", 14), "late payer")
    assert credit.sync_terms_from_xero(conn) == []
    assert _terms(conn, "Acme Widgets")[1] == "14 days"
    todo = credit.terms_to_write_to_xero(conn)
    assert [(r["display_name"], r["days"], r["kind"]) for r in todo] == [
        ("Acme Widgets", 14, "DAYSAFTERBILLDATE")
    ]
    credit.mark_terms_written(conn, ACME, 14, "DAYSAFTERBILLDATE")
    assert credit.terms_to_write_to_xero(conn) == []


def test_invoices_for_unknown_contacts_are_reported(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    stranger = UUID("00000000-0000-4000-8000-0000000f2999")
    assert _invoices(conn, ("PI-9", stranger, "2026-09-01", "2026-10-01")) == ["PI-9"]


def test_the_same_invoices_file_loads_once(conn: Connection, master_data: MasterDataIn) -> None:
    _setup(conn)
    _invoices(conn, ("PI-1", ACME, "2026-09-01", "2026-10-01"))
    with pytest.raises(SalesOrderError, match="already been loaded"):
        _invoices(conn, ("PI-1", ACME, "2026-09-01", "2026-10-01"))


@pytest.mark.parametrize(
    ("day", "basis", "text"),
    [
        (0, "DAYSAFTERBILLDATE", "due on invoice"),
        (60, "DAYSAFTERBILLDATE", "60 days"),
        (27, "OFFOLLOWINGMONTH", "by the 27th of the following month"),
        (1, "OFCURRENTMONTH", "by the 1st of the invoice month"),
        (12, "OFFOLLOWINGMONTH", "by the 12th of the following month"),
        (20, "DAYSAFTERBILLMONTH", "20 days after the end of the invoice month"),
    ],
)
def test_terms_in_plain_english(day: int, basis: str, text: str) -> None:
    assert credit.basis_text(day, basis) == text
