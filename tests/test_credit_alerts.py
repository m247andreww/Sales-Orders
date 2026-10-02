"""Bureau alert emails and the ARR file are read exactly; anything unrecognised is refused, never guessed."""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from conftest import FIXTURES
from openpyxl import Workbook

from sales_orders.credit_alerts import AlertFormatError, parse_alert
from sales_orders.credit_arr import ArrFileError, parse_arr_extract, parse_arr_file

EXPERIAN_HTML = (FIXTURES / "credit" / "experian_alert.html").read_text(encoding="utf-8")
CREDITSAFE_HTML = (FIXTURES / "credit" / "creditsafe_alert.html").read_text(encoding="utf-8")


def test_experian_alert_reads_limit_band_and_score() -> None:
    companies = {c.company_number: c for c in parse_alert("experian", EXPERIAN_HTML)}
    assert set(companies) == {"00000000", "99999991", "OC999999", "99999992"}
    test = companies["00000000"]
    # The workbook's "Experian suggested Credit Limit" is the Credit Limit, not the Credit Rating.
    assert (test.limit_status, test.credit_limit, test.previous_credit_limit) == (
        "value",
        Decimal(66000),
        Decimal(41000),
    )
    assert test.credit_rating == Decimal(33000)
    assert (test.risk_score, test.risk_band) == (57, "Below Average Risk")
    assert len(test.events) == 2


def test_experian_na_and_unreported_limits_are_distinguished() -> None:
    companies = {c.company_number: c for c in parse_alert("experian", EXPERIAN_HTML)}
    assert (companies["99999991"].limit_status, companies["99999991"].credit_limit) == ("not_available", None)
    assert companies["99999991"].risk_score is None  # "34 to N/A"
    assert companies["OC999999"].limit_status == "not_reported"  # a director change says nothing about limits
    assert (companies["99999992"].credit_limit, companies["99999992"].risk_band) == (
        Decimal(0),
        "Maximum Risk",
    )


def test_creditsafe_alert_groups_events_per_company() -> None:
    companies = {c.bureau_ref: c for c in parse_alert("creditsafe", CREDITSAFE_HTML)}
    assert set(companies) == {"UK00000001", "UK00000009"}
    test = companies["UK00000001"]
    assert test.company_number == "00000000"
    assert (test.limit_status, test.credit_limit, test.previous_credit_limit) == (
        "value",
        Decimal(52000),
        Decimal(40000),
    )
    assert test.events == ("Financial Changes", "Credit Limit; Previous 40000; New 52000")
    assert companies["UK00000009"].limit_status == "not_reported"


@pytest.mark.parametrize("bureau", ["experian", "creditsafe"])
def test_an_unrecognised_email_is_refused(bureau: str) -> None:
    with pytest.raises(AlertFormatError):
        parse_alert(bureau, "<p>Your invoice is attached</p>")


def test_each_format_is_only_read_by_its_own_parser() -> None:
    with pytest.raises(AlertFormatError):
        parse_alert("experian", CREDITSAFE_HTML)
    with pytest.raises(AlertFormatError):
        parse_alert("creditsafe", EXPERIAN_HTML)


def _arr_workbook(rows: list[list[object]], sheet: str = "Master Data") -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet
    ws.append(["Status", "Customer", "Internal\nRef.", "Product"])  # the file's two banner rows
    ws.append([None, None, None, None])
    ws.append(["Order Status", "Customer", "Internal Ref.", "Product", "Frequency", "Ann Rev"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_arr_file_columns_are_found_by_header_and_amounts_become_pence() -> None:
    content = _arr_workbook(
        [
            ["Live", "Test Customer", "TST001", "Support", "Monthly", 12000.004999],
            ["Order", "Test Customer", "TST002", "Licence", "Tri-Annual", 0.1 + 0.2],
            [None, None, None, None, None, None],  # blank row
            ["Live", "No Amount Ltd", "NOA001", "Support", "Annual", None],
        ]
    )
    parsed = parse_arr_file(content, "ARR.xlsx")
    assert [(ln.internal_ref, ln.frequency, ln.annual_revenue) for ln in parsed.lines] == [
        ("TST001", "monthly", Decimal("12000.00")),
        ("TST002", "tri-annual", Decimal("0.30")),
    ]
    assert parsed.skipped == ((7, "No Amount Ltd: no Ann Rev"),)
    assert len(parsed.sha256) == 64


def test_arr_file_without_the_expected_sheet_is_refused() -> None:
    with pytest.raises(ArrFileError, match="no 'Master Data' sheet"):
        parse_arr_file(_arr_workbook([], sheet="Other"), "ARR.xlsx")
    with pytest.raises(ArrFileError, match="not a readable"):
        parse_arr_file(b"not a workbook", "ARR.xlsx")


_EXTRACT = (
    "Workbook: 2 worksheets. Cell values are tab-separated rows.\n\n"
    "## Sheet: Credit Extract \u2014 4 rows \u00d7 5 columns (A1:E4)\n"
    "Order Status\tCustomer\tInternal Ref.\tFrequency\tAnn Rev\n"
    "Live\tTest Customer\tTST001\tMonthly\t1592.959984\n"
    "Cancelled\tTest Customer\tTST002\tAnnual\t300\n"
    "Live\tNo Amount Ltd\tNOA001\tAnnual\t\n"
    "Formulas:\nA1: =FILTER(...)\n\n"
    "## Sheet: Master Data \u2014 1230 rows \u00d7 54 columns (A1:BB1230)\n"
    "Status\tCustomer\n"
)


def test_connector_extract_is_read_in_full() -> None:
    parsed = parse_arr_extract(_EXTRACT, "ARR Live.xlsx")
    assert [(ln.internal_ref, ln.status, ln.frequency, ln.annual_revenue) for ln in parsed.lines] == [
        ("TST001", "Live", "monthly", Decimal("1592.96")),
        ("TST002", "Cancelled", "annual", Decimal("300.00")),
    ]
    assert parsed.skipped == ((4, "No Amount Ltd: no Ann Rev"),)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (_EXTRACT.replace("4 rows", "5 rows"), "announces 5 rows but 4 were read"),
        (_EXTRACT.replace("Live\tNo Amount", "[Output truncated]\nLive\tNo Amount"), "truncated"),
        (_EXTRACT.replace("Credit Extract", "Other"), "no 'Credit Extract' sheet"),
        (_EXTRACT.replace("\t300\n", "\t\u00a3300.00\n"), "Ann Rev is not a number"),  # a formatted value
    ],
)
def test_connector_extract_is_refused_unless_complete_and_raw(text: str, error: str) -> None:
    with pytest.raises(ArrFileError, match=error):
        parse_arr_extract(text, "ARR Live.xlsx")
