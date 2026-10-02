"""The daily scan for customers with recurring revenue but no credit monitoring (migration 0018).

All companies, names and figures are synthetic.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import urllib.request
from decimal import Decimal
from uuid import UUID

import pytest

from sales_orders import credit
from sales_orders.credit_arr import ArrLine, ParsedArrFile
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.service import load_master_data
from sales_orders.xero import (
    XeroClient,
    XeroCredentials,
    XeroDirectoryContact,
    XeroFormatError,
    fetch_contacts,
)

_seq = itertools.count()
NEWCO = UUID("00000000-0000-4000-8000-0000000e0001")


def _arr(conn: Connection, *lines: tuple[str, str, str]) -> int:
    """(customer name, internal ref, annual revenue) -> a loaded ARR snapshot (all Live, monthly)."""
    parsed = ParsedArrFile(
        source_name="ARR test.xlsx",
        sha256=hashlib.sha256(repr((lines, next(_seq))).encode()).hexdigest(),
        lines=tuple(
            ArrLine(i + 1, name, ref, "Live", "monthly", Decimal(amount))
            for i, (name, ref, amount) in enumerate(lines)
        ),
        skipped=(),
    )
    snapshot_id, _ = credit.load_arr_snapshot(conn, parsed)
    return snapshot_id


def _monitored(conn: Connection, name: str, prefix: str, number: str) -> None:
    load_master_data(
        conn,
        MasterDataIn(
            customers=(CustomerIn(legal_name=name, arr_prefix=prefix),),
            credit_subjects=(
                CreditSubjectIn(
                    display_name=name,
                    relationship="customer",
                    customer_legal_name=name,
                    company_number=number,
                ),
            ),
        ),
    )


def _directory(conn: Connection, *contacts: tuple[UUID, str, str | None]) -> None:
    credit.sync_xero_contacts(
        conn,
        [XeroDirectoryContact(cid, name, number, True, False, "ACTIVE") for cid, name, number in contacts],
    )


def test_unmonitored_customers_are_listed_and_new_ones_flagged(conn: Connection) -> None:
    _monitored(conn, "Known Client", "KNO", "00000001")
    _arr(conn, ("Known Client", "KNO001", "1000.00"), ("Old Unmonitored", "OLD001", "500.00"))
    _arr(
        conn,
        ("Known Client", "KNO001", "1000.00"),
        ("Old Unmonitored", "OLD001", "500.00"),
        ("Newco Services", "NEW001", "7000.00"),
        ("Newco Services", "NEW002", "1500.25"),
    )
    rows = credit.unmonitored_customers(conn)
    assert [(r["prefix"], r["new"], r["annual_revenue"], r["lines"]) for r in rows] == [
        ("NEW", True, "8500.25", 2),  # new ones first
        ("OLD", False, "500.00", 1),
    ]  # the monitored client is not listed


def test_xero_suggestion_only_on_a_single_match(conn: Connection) -> None:
    _directory(
        conn,
        (NEWCO, "Newco Services Ltd", "1234567"),  # Xero dropped the leading zero
        (UUID("00000000-0000-4000-8000-0000000e0002"), "Twin Ltd", None),
        (UUID("00000000-0000-4000-8000-0000000e0003"), "Twin Limited", None),
    )
    _arr(conn, ("Newco Services", "NEW001", "100.00"), ("Twin", "TWI001", "100.00"))
    by_prefix = {r["prefix"]: r["xero"] for r in credit.unmonitored_customers(conn)}
    assert by_prefix["NEW"] == {
        "xero_contact_id": str(NEWCO),
        "xero_name": "Newco Services Ltd",
        "company_number": "01234567",
    }
    assert by_prefix["TWI"] is None  # two candidates: never a guess


def test_adding_a_customer_to_monitoring_creates_the_client(conn: Connection) -> None:
    _directory(conn, (NEWCO, "Newco Services Ltd", "01234567"))
    _arr(conn, ("Newco Services", "NEW001", "100.00"))
    assert credit.add_monitored_customer(conn, "NEW", "1234567", str(NEWCO)) == "Newco Services"
    row = conn.execute(
        """SELECT c.arr_prefix, c.company_number, c.xero_contact_id, s.company_number AS subject_number
             FROM sales.credit_subject s JOIN sales.customer c USING (customer_id)
            WHERE s.display_name = 'Newco Services'"""
    ).fetchone()
    assert row == {
        "arr_prefix": "NEW",
        "company_number": "01234567",
        "xero_contact_id": NEWCO,
        "subject_number": "01234567",
    }
    assert credit.unmonitored_customers(conn) == []
    with pytest.raises(SalesOrderError, match="not an unmonitored customer"):
        credit.add_monitored_customer(conn, "NEW", "01234567")  # already monitored


def test_desk_lists_unmonitored_customers_once(conn: Connection) -> None:
    _arr(conn, ("Newco Services", "NEW001", "100.00"))
    desk = credit.desk_export(conn, credit.datetime.now(credit.LONDON))
    assert [u["prefix"] for u in desk["unmonitored"]] == ["NEW"]
    assert "ARR_NOT_MONITORED" not in {
        a["rule"] for a in desk["attention"]
    }  # not repeated in the attention list


def test_fetch_contacts_reads_every_page() -> None:
    def page(n: int, count: int) -> bytes:
        return json.dumps(
            {
                "Contacts": [
                    {"ContactID": str(UUID(int=n * 1000 + i)), "Name": f"Contact {n}-{i}", "IsCustomer": True}
                    for i in range(count)
                ]
            }
        ).encode()

    calls: list[str] = []

    def transport(request: urllib.request.Request) -> bytes:
        calls.append(request.full_url)
        if "connect/token" in request.full_url:
            return b'{"access_token": "t"}'
        return page(1, 100) if request.full_url.endswith("page=1") else page(2, 3)

    contacts = fetch_contacts(XeroCredentials("id", "secret"), transport)
    assert len(contacts) == 103
    assert [c for c in calls if "Contacts" in c][-1].endswith("page=2")


def test_researched_match_is_suggested_and_written_to_xero_once(conn: Connection) -> None:
    other = UUID("00000000-0000-4000-8000-0000000e0009")
    _directory(conn, (other, "Trading Name Ltd", None), (NEWCO, "Likely Co Ltd", None))
    _arr(conn, ("Shortname", "SHO001", "100.00"))  # no Xero contact matches "Shortname" by name
    credit.record_customer_match(
        conn,
        "SHO",
        source="PandaDoc application form (synthetic)",
        confidence="certain",
        xero_contact_id=str(other),
        registered_name="SHORTNAME TRADING LIMITED",
        company_number="7654321",
        note="Invoiced under its trading name",
    )
    [row] = credit.unmonitored_customers(conn)
    assert row["xero"] == {
        "xero_contact_id": str(other),
        "xero_name": "Trading Name Ltd",
        "company_number": "07654321",
    }
    assert row["match"]["registered_name"] == "SHORTNAME TRADING LIMITED"
    assert row["match"]["note"] == "Invoiced under its trading name"

    credit.record_customer_match(
        conn,
        "LIK",
        source="web (synthetic)",
        confidence="likely",
        xero_contact_id=str(NEWCO),
        company_number="01111111",
    )
    assert [r["arr_prefix"] for r in credit.matches_to_write_to_xero(conn)] == ["SHO"]  # never a "likely" one
    credit.mark_written_to_xero(conn, "SHO")
    assert credit.matches_to_write_to_xero(conn) == []  # never written twice
    number = conn.execute(
        "SELECT company_number FROM sales.xero_contact_directory WHERE xero_contact_id = %s", (other,)
    ).fetchone()
    assert number == {"company_number": "07654321"}


def test_excluded_customer_leaves_the_list_but_is_reported(conn: Connection) -> None:
    _arr(conn, ("Gone Ltd", "GON001", "900.00"), ("Kept Ltd", "KEP001", "100.00"))
    credit.record_customer_match(
        conn, "GON", source="CFO (synthetic)", confidence="certain", excluded_reason="In administration"
    )
    assert [r["prefix"] for r in credit.unmonitored_customers(conn)] == ["KEP"]
    desk = credit.desk_export(conn, credit.datetime.now(credit.LONDON))
    assert desk["unmonitored_excluded"] == [
        {"prefix": "GON", "name": "Gone Ltd", "reason": "In administration"}
    ]


def test_set_company_number_sends_only_that_field() -> None:
    sent: list[dict[str, object]] = []

    def transport(request: urllib.request.Request) -> bytes:
        if "connect/token" in request.full_url:
            return b'{"access_token": "t"}'
        assert request.data is not None
        body = json.loads(request.data)
        sent.append(body)
        return json.dumps(
            {"Contacts": [{"ContactID": str(NEWCO), "CompanyNumber": body["Contacts"][0]["CompanyNumber"]}]}
        ).encode()

    XeroClient(XeroCredentials("id", "secret"), transport).set_company_number(NEWCO, "01234567")
    assert sent == [{"Contacts": [{"ContactID": str(NEWCO), "CompanyNumber": "01234567"}]}]

    def refuses(request: urllib.request.Request) -> bytes:
        return b'{"access_token": "t"}' if "connect/token" in request.full_url else b'{"Contacts": [{}]}'

    with pytest.raises(XeroFormatError):
        XeroClient(XeroCredentials("id", "secret"), refuses).set_company_number(NEWCO, "01234567")
