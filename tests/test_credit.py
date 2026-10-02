"""Credit & risk management (migration 0014): the workbook's arithmetic, its fixes, the authority
rules on limits, the filing outbox and the daily run. All companies and figures are synthetic."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest
from conftest import CFO, FINANCE, act_as, savepoint_rejects

from sales_orders import credit, credit_run
from sales_orders.credit_arr import ArrLine, ParsedArrFile
from sales_orders.credit_pdf import render, render_alert
from sales_orders.db import Connection
from sales_orders.errors import UnknownReferenceError
from sales_orders.graph import MailMessage
from sales_orders.models import CreditSubjectIn, MasterDataIn
from sales_orders.service import load_master_data

CUSTOMER = "Test Customer Ltd"  # fixture customer: ARR prefix TST, company 00000000, has a Xero contact
SUBJECT = "Test Customer"
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_seq = iter(range(1, 10_000))


# ---------------------------------------------------------------------------- helpers


def _subject(
    conn: Connection,
    name: str = SUBJECT,
    relationship: str = "customer",
    company_number: str | None = "00000000",
    allowance: str | None = "10000",
    **extra: Any,
) -> int:
    load_master_data(
        conn,
        MasterDataIn(
            credit_subjects=(
                CreditSubjectIn(
                    display_name=name,
                    relationship=relationship,
                    company_number=company_number,
                    customer_legal_name=CUSTOMER if relationship == "customer" else None,
                    one_off_allowance=Decimal(allowance) if allowance is not None else None,
                    **extra,
                ),
            )
        ),
    )
    row = conn.execute(
        "SELECT credit_subject_id FROM sales.credit_subject WHERE display_name = %s", (name,)
    ).fetchone()
    assert row is not None
    return int(row["credit_subject_id"])


def _arr(conn: Connection, *lines: tuple[str, str, str, str]) -> int:
    """(internal ref, status, frequency, annual revenue) -> a loaded ARR snapshot."""
    parsed = ParsedArrFile(
        source_name="ARR test.xlsx",
        sha256=hashlib.sha256(repr((lines, next(_seq))).encode()).hexdigest(),
        lines=tuple(
            ArrLine(i + 1, "Test Customer", ref, status, freq, Decimal(amount))
            for i, (ref, status, freq, amount) in enumerate(lines)
        ),
        skipped=(),
    )
    snapshot_id, created = credit.load_arr_snapshot(conn, parsed)
    assert created
    return snapshot_id


def _experian_company(number: str, name: str, limit: str | None = None, band: str | None = None) -> str:
    items = []
    if limit is not None:
        items.append(
            f"<li>The Credit Rating has changed from £1,000 to £1,000 and Credit Limit has changed from £1 to {limit}. "
            "The new Credit Rating and Credit Limit are now available to view on Experian Business Express.</li>"
        )
    if band is not None:
        items.append(
            "<li>The Credit Risk Score has changed from 50 to 40. The new Credit Risk Score is now available to view on "
            f"Experian Business Express and the Credit Risk Band has moved from Below Average Risk to {band}.</li>"
        )
    if not items:
        items.append("<li>There has been a change relating to the directors of this business.</li>")
    return (
        f'<p><strong><a href="https://protect.businessexpress-uk.com/reg-report/{number}/change-history">'
        f"{number} : {name}</a></strong></p><ul>{''.join(items)}</ul>"
    )


def _experian_html(*companies: str) -> str:
    return (
        "<p>Experian Business Express</p><h3>Registered Companies</h3>"
        + "".join(companies)
        + "<h3>Non-Registered Companies</h3>"
    )


def _creditsafe_html(number: str, ref: str, name: str, new: str) -> str:
    return (
        "<h1>Monitoring Alert.</h1><table><tbody><tr><th>Company Name<br />Company No. / Safe No.</th>"
        "<th>Reference</th><th>Personal Limit</th><th>Notes</th><th>Event Details</th></tr></tbody><tbody><tr>"
        f'<td><a href="https://app.creditsafe.com/companies/{ref}">{name}</a><br />{number} / {ref}</td>'
        f"<td></td><td></td><td></td><td>Credit Limit<br />Previous Value: 1<br />New Value: {new}</td>"
        "</tr></tbody></table>"
    )


def _alert(conn: Connection, bureau: str, html: str, received: datetime = NOW) -> credit.AlertResult:
    return credit.store_alert(
        conn,
        bureau=bureau,
        internet_message_id=f"<{next(_seq)}@test.example>",
        received_at=received,
        subject="alert",
        html=html,
        mailbox_message_id=f"graph-{next(_seq)}",
    )


def _bureaus(
    conn: Connection, experian: str | None, creditsafe: str | None, received: datetime = NOW
) -> None:
    if experian is not None:
        _alert(
            conn,
            "experian",
            _experian_html(_experian_company("00000000", "TEST CUSTOMER LIMITED", experian)),
            received,
        )
    if creditsafe is not None:
        _alert(
            conn,
            "creditsafe",
            _creditsafe_html("00000000", "UK00000001", "TEST CUSTOMER LIMITED", creditsafe),
            received,
        )


def _limit(conn: Connection) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT credit_limit, source_code FROM sales.v_customer_current_credit_limit l
             JOIN sales.customer c USING (customer_id) WHERE c.legal_name = %s""",
        (CUSTOMER,),
    ).fetchone()


def _rules(conn: Connection, name: str | None = None) -> set[str]:
    rows = conn.execute(
        "SELECT rule_code FROM sales.v_credit_exception WHERE %s::text IS NULL OR display_name = %s",
        (name, name),
    ).fetchall()
    return {r["rule_code"] for r in rows}


@pytest.fixture
def worked_example(conn: Connection, master_data: MasterDataIn) -> dict[str, Any]:
    """The CFO's worked example (company anonymised): Experian £57,000, Creditsafe £75,000, £5,300 annual ARR, £10,000 one-off."""
    _subject(conn)
    _arr(conn, ("TST001", "Live", "annual", "5300.00"))
    _bureaus(conn, "£57,000", "75000")
    done = credit.assess_due(conn)
    assert len(done) == 1
    return done[0]


# ---------------------------------------------------------------------------- the workbook's arithmetic


def test_worked_example_reproduces_the_workbook(conn: Connection, worked_example: dict[str, Any]) -> None:
    a = worked_example
    assert (a["baseline"], a["recurring_exposure"], a["net_requirement"]) == (
        Decimal(57000),
        Decimal(5300),
        Decimal(15300),
    )
    assert (a["vat"], a["gross_requirement"], a["trading_requirement"]) == (
        Decimal(3060),
        Decimal(18360),
        Decimal(18400),
    )
    assert (a["outcome_code"], a["trigger_code"]) == ("applied", "initial")
    assert _limit(conn) == {"credit_limit": Decimal(18400), "source_code": "assessment"}


def test_exposure_per_frequency_counts_only_commitments(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn, allowance="0")
    _arr(
        conn,
        ("TST001", "Live", "monthly", "12000.00"),  # two months: 2,000
        ("TST002", "Order", "quarterly", "4000.00"),  # one quarter: 1,000
        ("TST003", "Renewal - New", "annual", "300.00"),  # one year: 300
        ("TST004", "Live", "tri-annual", "600.00"),  # multi-year at one year's value (workbook): 600
        ("TST005", "Live", "6 months", "1000.00"),  # one half-year: 500 (the workbook ignored this frequency)
        ("TST006", "Cancelled", "monthly", "99999.00"),  # the workbook counted cancelled lines
        ("TST007", "Renewal - Old", "annual", "88888.00"),  # ...and both halves of a renewal
        ("OTH001", "Live", "annual", "77777.00"),  # another customer's prefix
    )
    _bureaus(conn, "£1,000,000", "1000000")
    a = credit.assess_due(conn)[0]
    lines = {
        r["arr_frequency"]: r["exposure"]
        for r in conn.execute(
            "SELECT arr_frequency, exposure FROM sales.credit_assessment_line WHERE credit_assessment_id = %s",
            (a["credit_assessment_id"],),
        )
    }
    assert lines == {
        "monthly": Decimal(2000),
        "quarterly": Decimal(1000),
        "annual": Decimal(300),
        "tri-annual": Decimal(600),
        "6 months": Decimal(500),
    }
    assert a["recurring_exposure"] == Decimal(4400)
    assert a["trading_requirement"] == Decimal(5300)  # 4,400 + 20% VAT = 5,280 -> rounded up to 5,300


def test_requirement_outside_appetite_waits_for_the_cfo(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn, allowance="8000")
    _arr(conn)
    _bureaus(conn, "£10,000", "19000")  # baseline 10,000: appetite 5,000; requirement 9,600
    a = credit.assess_due(conn)[0]
    assert a["outcome_code"] == "cfo_review"
    assert "exceeds risk appetite £5,000" in a["review_reason"]
    assert _limit(conn) is None
    assert "CREDIT_REVIEW_NEEDED" in _rules(conn, SUBJECT)

    act_as(conn, FINANCE)
    with pytest.raises(psycopg.Error, match="setting a credit limit is not permitted"), conn.transaction():
        credit.decide(conn, SUBJECT, Decimal(9600), "trading need")
    act_as(conn, CFO)
    credit.decide(conn, SUBJECT, Decimal(9600), "long-standing client, pays by DD", date(2027, 3, 31))
    assert _limit(conn) == {"credit_limit": Decimal(9600), "source_code": "cfo_decision"}
    assert "CREDIT_REVIEW_NEEDED" not in _rules(conn, SUBJECT)


def test_no_bureau_limit_or_adverse_band_needs_review(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn, allowance="1000")
    _arr(conn)
    _bureaus(conn, "£0", None)
    a = credit.assess_due(conn)[0]
    assert a["review_reason"] == "no bureau credit limit to support the trading requirement"

    _alert(
        conn,
        "experian",
        _experian_html(_experian_company("00000000", "TEST CUSTOMER LIMITED", "£900,000", "Maximum Risk")),
    )
    a = credit.assess_due(conn)[0]
    assert (a["trigger_code"], a["outcome_code"]) == ("bureau_change", "cfo_review")
    assert a["review_reason"] == "Experian risk band is Maximum Risk"
    assert "ADVERSE_RISK_BAND" in _rules(conn, SUBJECT)


def test_reassessed_only_when_an_input_changes(conn: Connection, worked_example: dict[str, Any]) -> None:
    assert credit.assess_due(conn) == []  # nothing changed
    _alert(
        conn, "experian", _experian_html(_experian_company("00000000", "TEST CUSTOMER LIMITED"))
    )  # director change only
    assert credit.assess_due(conn) == []

    _bureaus(conn, None, "80000", NOW + timedelta(days=1))
    a = credit.assess_due(conn)
    assert [(x["trigger_code"], x["creditsafe_limit"], x["outcome_code"]) for x in a] == [
        ("bureau_change", Decimal(80000), "unchanged")  # baseline still 57,000: same requirement, same limit
    ]

    _arr(conn, ("TST001", "Live", "annual", "5300.00"), ("TST002", "Order", "annual", "10000.00"))
    a = credit.assess_due(conn)
    assert [(x["trigger_code"], x["trading_requirement"], x["outcome_code"]) for x in a] == [
        ("arr_change", Decimal(30400), "cfo_review")  # 25,300 + VAT = 30,360 -> 30,400 > appetite 28,500
    ]


def test_a_cfo_decision_in_force_is_not_overwritten(conn: Connection, worked_example: dict[str, Any]) -> None:
    act_as(conn, CFO)
    credit.decide(conn, SUBJECT, Decimal(100000), "per independent Board discussions", date(2099, 1, 1))
    _bureaus(conn, "£60,000", None, NOW + timedelta(days=1))
    a = credit.assess_due(conn)[0]
    assert a["outcome_code"] == "override_in_force"
    assert _limit(conn) == {"credit_limit": Decimal(100000), "source_code": "cfo_decision"}


# ---------------------------------------------------------------------------- the limit register


def test_automatic_limit_must_be_the_recommendation(conn: Connection, worked_example: dict[str, Any]) -> None:
    msg = savepoint_rejects(
        conn,
        """INSERT INTO sales.customer_credit_limit (customer_id, credit_limit, source_code, credit_assessment_id)
           SELECT customer_id, 50000, 'assessment', %s FROM sales.customer WHERE legal_name = %s""",
        (worked_example["credit_assessment_id"], CUSTOMER),
    )
    assert msg == "an automatic credit limit must be the recommendation of an assessment within appetite"


def test_limits_are_history(conn: Connection, worked_example: dict[str, Any]) -> None:
    msg = savepoint_rejects(conn, "UPDATE sales.customer_credit_limit SET credit_limit = 1")
    assert msg.startswith("credit limits are history")
    act_as(conn, CFO)
    credit.decide(conn, SUBJECT, Decimal(20000), "seasonal peak")
    periods = conn.execute(
        "SELECT credit_limit, effective_to IS NULL AS open FROM sales.customer_credit_limit ORDER BY 1"
    ).fetchall()
    assert periods == [
        {"credit_limit": Decimal(18400), "open": False},
        {"credit_limit": Decimal(20000), "open": True},
    ]
    msg = savepoint_rejects(
        conn, "UPDATE sales.customer_credit_limit SET effective_to = now() WHERE credit_limit = 18400"
    )
    assert msg.startswith("credit limits are history")


def test_credit_terms_no_longer_hold_a_limit(conn: Connection, master_data: MasterDataIn) -> None:
    msg = savepoint_rejects(conn, "UPDATE sales.customer_credit_terms SET credit_limit = 1000")
    assert "customer_credit_terms_no_limit" in msg


def test_evidence_is_append_only(conn: Connection, worked_example: dict[str, Any]) -> None:
    for table in ("credit_report", "credit_alert_email", "credit_assessment", "credit_arr_line"):
        msg = savepoint_rejects(conn, f"DELETE FROM sales.{table}")  # noqa: S608 - fixed table names
        assert msg == f"{table} is append-only evidence: DELETE is not allowed"


# ---------------------------------------------------------------------------- inputs


def test_alerts_are_idempotent_and_unknown_companies_reported(
    conn: Connection, master_data: MasterDataIn
) -> None:
    html = _experian_html(_experian_company("99999999", "STRANGER LIMITED", "£5,000"))
    first = credit.store_alert(
        conn, bureau="experian", internet_message_id="<x@y>", received_at=NOW, subject="s", html=html
    )
    again = credit.store_alert(
        conn, bureau="experian", internet_message_id="<x@y>", received_at=NOW, subject="s", html=html
    )
    assert (first.created, again.created, again.credit_alert_email_id) == (
        True,
        False,
        first.credit_alert_email_id,
    )
    assert _rules(conn) >= {"UNKNOWN_COMPANY"}


def test_an_unreadable_alert_is_recorded_and_reported(conn: Connection, master_data: MasterDataIn) -> None:
    r = _alert(conn, "creditsafe", "<p>new layout</p>")
    assert (r.created, r.companies) == (True, 0)
    assert r.parse_error is not None
    assert "ALERT_NOT_READ" in _rules(conn)


def test_arr_load_refuses_an_unknown_frequency(conn: Connection, master_data: MasterDataIn) -> None:
    with pytest.raises(UnknownReferenceError, match="fortnightly"):
        _arr(conn, ("TST001", "Live", "fortnightly", "1.00"))


def test_workbook_import_seeds_readings_and_allowance(conn: Connection, master_data: MasterDataIn) -> None:
    sid = _subject(conn, company_number=None, allowance=None, workbook_sheet="TST")
    sheets = [
        credit.WorkbookSheet(
            "TST",
            (Decimal(57000), date(2026, 8, 18)),
            (Decimal(75000), date(2026, 8, 18)),
            Decimal(2500),
            None,
        ),
        credit.WorkbookSheet("XXX", None, None, None, None),
    ]
    r = credit.import_workbook(conn, sheets)
    assert r["unmatched_sheets"] == ["XXX"]
    credit.import_workbook(conn, sheets)  # re-running changes nothing
    pos = conn.execute(
        "SELECT bureau_code, credit_limit FROM sales.v_credit_bureau_position WHERE credit_subject_id = %s ORDER BY 1",
        (sid,),
    ).fetchall()
    assert pos == [
        {"bureau_code": "creditsafe", "credit_limit": Decimal(75000)},
        {"bureau_code": "experian", "credit_limit": Decimal(57000)},
    ]
    allowance = conn.execute(
        "SELECT one_off_allowance FROM sales.credit_subject WHERE credit_subject_id = %s", (sid,)
    )
    assert allowance.fetchone() == {"one_off_allowance": Decimal(2500)}


# ---------------------------------------------------------------------------- exceptions


@pytest.mark.parametrize(
    ("setup", "rule"),
    [
        (lambda c: _bureaus(c, "£1,000", None), "SINGLE_BUREAU"),
        (lambda c: None, "NO_BUREAU_LIMIT"),
        (lambda c: _bureaus(c, "£1,000", "1000", NOW - timedelta(days=400)), "STALE_BUREAU_DATA"),
        (lambda c: c.execute("UPDATE sales.customer SET xero_contact_id = NULL"), "CUSTOMER_NO_XERO_CONTACT"),
        (lambda c: c.execute("UPDATE sales.customer SET arr_prefix = NULL"), "CUSTOMER_NO_ARR_PREFIX"),
    ],
)
def test_subject_exception_rules(conn: Connection, master_data: MasterDataIn, setup: Any, rule: str) -> None:
    _subject(conn)
    assert rule not in _rules(conn, SUBJECT) or rule == "NO_BUREAU_LIMIT"
    setup(conn)
    assert rule in _rules(conn, SUBJECT)


def test_folder_and_bureau_rules_clear_when_fixed(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn)
    assert "NO_BUREAU_LIMIT" in _rules(conn, SUBJECT)
    _bureaus(conn, "£1,000", "1000")
    assert _rules(conn, SUBJECT) & {"NO_BUREAU_LIMIT", "SINGLE_BUREAU"} == set()


def test_arr_revenue_without_monitoring_is_reported(conn: Connection, master_data: MasterDataIn) -> None:
    _arr(conn, ("ZZZ001", "Live", "monthly", "1200.00"), ("TST001", "Cancelled", "monthly", "5.00"))
    messages = conn.execute(
        "SELECT message FROM sales.v_credit_exception WHERE rule_code = 'ARR_NOT_MONITORED'"
    )
    assert [r["message"] for r in messages] == [
        "ARR prefix ZZZ (Test Customer): £1,200.00 a year of commitments, no credit monitoring"
    ]


def test_cfo_decision_past_review_is_reported(conn: Connection, worked_example: dict[str, Any]) -> None:
    act_as(conn, CFO)
    credit.decide(conn, SUBJECT, Decimal(30000), "temporary uplift", date(2020, 1, 1))
    assert "CREDIT_DECISION_REVIEW_DUE" in _rules(conn, SUBJECT)


# ---------------------------------------------------------------------------- filing and the daily run


class FakeMail:
    def __init__(self, messages: list[MailMessage], fail_copy: bool = False) -> None:
        self.messages, self.fail_copy = messages, fail_copy
        self.copied: list[tuple[str, str]] = []
        self.sent: list[str] = []

    def messages_from(self, mailbox: str, sender: str, since: datetime) -> list[MailMessage]:
        return [m for m in self.messages if m.sender == sender and m.received_at >= since]

    def copy_message(self, mailbox: str, graph_id: str, folder_id: str) -> str:
        if self.fail_copy:
            raise RuntimeError("mailbox unavailable")
        self.copied.append((graph_id, folder_id))
        return f"copy-of-{graph_id}"

    def send_mail(self, mailbox: str, to: str, subject: str, html: str) -> None:
        self.sent.append(subject)

    def download_shared_file(self, sharing_url: str) -> tuple[bytes, datetime | None, str]:
        raise RuntimeError("not used")


class FakeXero:
    def __init__(self) -> None:
        self.attached: list[tuple[UUID, str, bytes, str]] = []
        self.notes: list[str] = []

    def attach_to_contact(
        self, contact_id: UUID, file_name: str, content: bytes, idempotency_key: str
    ) -> str:
        self.attached.append((contact_id, file_name, content, idempotency_key))
        return "00000000-0000-4000-8000-0000000a7001"

    def add_contact_note(self, contact_id: UUID, details: str, idempotency_key: str) -> None:
        self.notes.append(details)


def _uow(conn: Connection) -> credit_run.UnitOfWork:
    @contextmanager
    def unit() -> Iterator[Connection]:
        with conn.transaction():
            yield conn

    return unit


def test_daily_run_assesses_files_and_reports(conn: Connection, master_data: MasterDataIn) -> None:
    received = datetime.now(UTC) - timedelta(minutes=5)
    _subject(conn)
    conn.execute("UPDATE sales.credit_subject SET debt_credit_folder_id = 'folder-test'")
    _subject(conn, name="Example Supplies", relationship="information", company_number="99999991")
    _arr(conn, ("TST001", "Live", "annual", "5300.00"))
    mail = FakeMail(
        [
            MailMessage(
                "g1",
                "<e1@x>",
                "ebe.noreply@experian.com",
                "New Experian Business Express Alerts",
                received,
                _experian_html(
                    _experian_company("00000000", "TEST CUSTOMER LIMITED", "£57,000"),
                    _experian_company("99999991", "EXAMPLE SUPPLIES LIMITED", "£5,000"),
                ),
            ),
            MailMessage(
                "g2",
                "<e2@x>",
                "monitoring@creditsafe.com",
                "Connect monitoring alert",
                received,
                _creditsafe_html("00000000", "UK00000001", "TEST CUSTOMER LIMITED", "75000"),
            ),
        ]
    )
    xero = FakeXero()
    settings = credit_run.RunSettings(
        mailbox="cfo@example.com", arr_file_url=None, summary_to="cfo@example.com"
    )

    report = credit_run.run(_uow(conn), settings, mail, xero)
    assert report.errors == []
    assert report.alerts_read == 2
    assert [(a["display_name"], a["outcome_code"], a["trading_requirement"]) for a in report.assessments] == [
        (SUBJECT, "applied", Decimal(18400))  # the information-only company is not assessed
    ]
    # Snapshot + note on the Xero contact, plus the client's part of each alert (never the supplier's).
    # Outlook folder copies are retired (migration 0017): nothing is copied in the mailbox.
    aid = report.assessments[0]["credit_assessment_id"]
    attached = sorted((str(x[0]), x[1], x[2][:5], x[3]) for x in xero.attached)
    assert [(a[0], a[2]) for a in attached] == [("00000000-0000-4000-8000-00000000c001", b"%PDF-")] * 3
    keys = sorted(a[3] for a in attached)
    assert keys[2] == f"credit-assessment-{aid}"
    assert all(k.startswith("credit-alert-") for k in keys[:2]) and keys[0] != keys[1]
    names = [a[1] for a in attached]
    assert sum(n.endswith("__18400.pdf") for n in names) == 1
    assert sorted(n.split("_Alert_")[1].split("_")[0] for n in names if "_Alert_" in n) == [
        "Creditsafe",
        "Experian",
    ]
    assert len(xero.notes) == 1  # a note for the assessment only, none for alerts
    assert xero.notes[0].startswith("Credit limit £18,400")
    assert mail.copied == []
    assert mail.sent == ["Credit run: 1 limit(s) updated, 0 decision(s) needed"]

    again = credit_run.run(_uow(conn), settings, mail, xero)  # idempotent: nothing new
    assert (again.alerts_read, again.assessments, again.filed) == (0, [], [])
    assert len(xero.attached) == 3  # nothing filed twice


def test_failed_filing_is_retried_then_reported(conn: Connection, master_data: MasterDataIn) -> None:
    received = datetime.now(UTC) - timedelta(minutes=5)
    _subject(conn)
    conn.execute("UPDATE sales.credit_subject SET debt_credit_folder_id = 'folder-test'")
    _arr(conn)
    mail = FakeMail(
        [
            MailMessage(
                "g1",
                "<e1@x>",
                "monitoring@creditsafe.com",
                "alert",
                received,
                _creditsafe_html("00000000", "UK00000001", "TEST CUSTOMER LIMITED", "75000"),
            )
        ],
        fail_copy=True,
    )
    settings = credit_run.RunSettings(mailbox="cfo@example.com", arr_file_url=None, summary_to=None)
    for _ in range(credit.MAX_FILING_ATTEMPTS):
        report = credit_run.run(_uow(conn), settings, mail, None)  # Xero not configured either
        assert len(report.filing_errors) == 2  # the folder copy and the Xero snapshot, every run
    assert "FILING_FAILED" in _rules(conn, SUBJECT)
    assert credit.pending_filing(conn) == []
    assert credit.retry_failed_filing(conn) == 2


def test_snapshot_is_made_once_and_reproducible(conn: Connection, worked_example: dict[str, Any]) -> None:
    aid = int(worked_example["credit_assessment_id"])
    name, pdf = credit.ensure_snapshot(conn, aid)
    assert name.startswith("Test_Customer_CLA_") and name.endswith(f"_{aid}__18400.pdf")
    assert credit.ensure_snapshot(conn, aid) == (name, pdf)
    assert render(credit.snapshot_data(conn, aid)) == pdf  # same input, same bytes
    row = conn.execute(
        "SELECT sha256 FROM sales.credit_assessment_snapshot WHERE credit_assessment_id = %s", (aid,)
    )
    assert row.fetchone() == {"sha256": hashlib.sha256(pdf).hexdigest()}


def test_only_the_latest_assessment_is_filed_on_xero(
    conn: Connection, worked_example: dict[str, Any]
) -> None:
    first = int(worked_example["credit_assessment_id"])
    credit.queue_filing(conn)
    _arr(conn, ("TST001", "Live", "annual", "9000.00"))  # the same day, before anything is filed
    [second] = credit.assess_due(conn)
    credit.queue_filing(conn)
    pending = [t for t in credit.pending_filing(conn) if t["task_code"] == "xero_snapshot"]
    assert [t["credit_assessment_id"] for t in pending] == [second["credit_assessment_id"]]
    row = conn.execute(
        "SELECT status_code FROM sales.credit_filing_task WHERE credit_assessment_id = %s", (first,)
    ).fetchone()
    assert row == {"status_code": "superseded"}  # kept as a record, never deleted


def test_alert_is_filed_on_xero_for_clients_only(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn)
    _subject(conn, name="Example Supplies", relationship="information", company_number="99999991")
    _alert(
        conn,
        "experian",
        _experian_html(
            _experian_company("00000000", "TEST CUSTOMER LIMITED", "£57,000"),
            _experian_company("99999991", "EXAMPLE SUPPLIES LIMITED", "£5,000"),
        ),
    )
    credit.queue_filing(conn)
    tasks = [t for t in credit.pending_filing(conn) if t["credit_alert_email_id"] is not None]
    assert [(t["task_code"], t["display_name"]) for t in tasks] == [("xero_alert", SUBJECT)]  # no mail_copy

    name, pdf = credit.ensure_alert_snapshot(
        conn, tasks[0]["credit_alert_email_id"], tasks[0]["credit_subject_id"]
    )
    assert name.startswith("Test_Customer_Alert_Experian_") and pdf[:5] == b"%PDF-"
    data = credit.alert_snapshot_data(conn, tasks[0]["credit_alert_email_id"], tasks[0]["credit_subject_id"])
    assert [c.company_name for c in data.companies] == ["TEST CUSTOMER LIMITED"]  # only this client's lines
    assert data.companies[0].credit_limit == Decimal(57000)
    assert render_alert(data) == pdf  # same input, same bytes
    assert credit.ensure_alert_snapshot(
        conn, tasks[0]["credit_alert_email_id"], tasks[0]["credit_subject_id"]
    ) == (
        name,
        pdf,
    )


def test_xero_note_says_when_a_bureau_gave_no_limit(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn)
    _arr(conn, ("TST001", "Live", "annual", "5300.00"))
    _bureaus(conn, "£57,000", None)  # Creditsafe has not reported this company
    [a] = credit.assess_due(conn)
    note = credit.xero_note(conn, int(a["credit_assessment_id"]))
    assert "Experian £57,000, Creditsafe no limit reported," in note


def test_debt_and_credit_folders_are_mapped_by_client_name(
    conn: Connection, master_data: MasterDataIn
) -> None:
    _subject(conn)
    folders = [
        ("f-1", ("Inbox", "Clients", "Test Customer Ltd", "Debt & Credit")),
        ("f-2", ("Inbox", "Clients", "Another Client", "Debt & Credit")),
        ("f-3", ("Inbox", "Clients", "Test Customer Ltd", "Orders")),
    ]
    r = credit.map_folders(conn, folders)
    assert r["mapped"] == [{"subject": SUBJECT, "folder": "Inbox/Clients/Test Customer Ltd/Debt & Credit"}]
    assert credit.map_folders(conn, folders)["mapped"] == []  # already mapped: never changed


def test_alert_whose_previous_limit_disagrees_is_flagged(
    conn: Connection, worked_example: dict[str, Any]
) -> None:
    def experian(previous: str, new: str, when: datetime) -> None:
        body = _experian_company("00000000", "TEST CUSTOMER LIMITED", new).replace(
            "from £1 to", f"from {previous} to"
        )
        _alert(conn, "experian", _experian_html(body), when)

    experian("£57,000", "£58,000", NOW + timedelta(days=1))  # continuous with the last reading
    assert "ALERT_CONTINUITY" not in _rules(conn, SUBJECT)
    experian("£70,000", "£71,000", NOW + timedelta(days=2))  # says 70,000 but we last recorded 58,000
    messages = conn.execute(
        "SELECT message FROM sales.v_credit_exception WHERE rule_code = 'ALERT_CONTINUITY'"
    ).fetchall()
    assert [m["message"] for m in messages] == [
        "experian alert of 2026-10-03 says the previous limit was £70,000; the last recorded limit was £58,000"
        " (missed alert or misread)"
    ]


def test_summary_reports_today(conn: Connection, worked_example: dict[str, Any]) -> None:
    report = credit_run.report_since(conn, NOW - timedelta(days=3650))
    assert [a["credit_assessment_id"] for a in report.assessments] == [worked_example["credit_assessment_id"]]
    assert credit_run.summary_subject(report) == "Credit run: 1 limit(s) updated, 0 decision(s) needed"
    assert "Test Customer: applied - requirement £18,400" in credit_run.summary_html(report)


def test_workbook_figures_round_trip_through_json() -> None:
    sheets = [
        credit.WorkbookSheet(
            "TST",
            (Decimal("57000.00"), date(2026, 8, 18)),
            (None, date(2026, 8, 18)),
            Decimal("10000.00"),
            None,
        ),
        credit.WorkbookSheet("XXX", None, None, None, Decimal("100000.00")),
    ]
    assert credit.workbook_from_json(credit.workbook_to_json(sheets)) == sheets
    with pytest.raises(ValueError, match="amounts must be strings"):
        credit.workbook_from_json(
            '[{"sheet": "A", "experian": null, "creditsafe": null, "one_off": 5.5, "workbook_limit": null}]'
        )


def test_desk_export_lists_decisions_and_changes(conn: Connection, master_data: MasterDataIn) -> None:
    _subject(conn, allowance="8000")
    _arr(conn)
    _bureaus(conn, "£10,000", "19000")  # requirement 9,600 > appetite 5,000: needs the CFO
    credit.assess_due(conn)
    desk = credit.desk_export(conn, NOW - timedelta(days=3650))
    assert [(r["company"], r["why"], r["trading_need"], r["half_lower"]) for r in desk["review"]] == [
        (SUBJECT, "Trading need is above half the lower bureau limit", "9600.00", "5000.00")
    ]
    assert desk["changed"] == [] and len(desk["alerts"]) == 2
    act_as(conn, CFO)
    credit.decide(conn, SUBJECT, Decimal(9600), "pays by Direct Debit")
    assert credit.desk_export(conn, NOW - timedelta(days=3650))["review"] == []  # decided: off the list
