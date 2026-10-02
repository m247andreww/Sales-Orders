"""The daily credit routine: one command, no manual steps (sales-orders credit-run).

  1. ARR file   - read from SharePoint; a new version is loaded as a snapshot.
  2. Alerts     - every Experian / Creditsafe alert since the last one read; each company's reading stored.
  3. Assess     - every monitored company whose bureau limit, risk band, ARR requirement or allowance
                  changed; limits within risk appetite are applied, the rest wait for the CFO.
  4. File       - snapshot PDF + history note on the customer's Xero contact; each alert email copied
                  into each client's Debt & Credit folder. Retried on the next run if anything fails.
  5. Summary    - emailed to the CFO: what changed, what needs a decision, every exception.

Each step commits on its own, so a failure in one (e.g. Xero down) never loses another's work, and
re-running is always safe (every step is idempotent).
"""

from __future__ import annotations

import html
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sales_orders import credit
from sales_orders.credit_arr import parse_arr_file
from sales_orders.db import Connection
from sales_orders.graph import MailMessage

UnitOfWork = Callable[[], AbstractContextManager[Connection]]
FIRST_RUN_LOOKBACK = timedelta(
    days=1
)  # first run: only alerts from the last day (older ones were filed by hand)
OVERLAP = timedelta(hours=1)  # later runs re-read an hour before the last alert: Message-ID makes it safe


class Mailbox(Protocol):
    def messages_from(self, mailbox: str, sender: str, since: datetime) -> list[MailMessage]: ...
    def copy_message(self, mailbox: str, graph_id: str, folder_id: str) -> str: ...
    def send_mail(self, mailbox: str, to: str, subject: str, html: str) -> None: ...
    def download_shared_file(self, sharing_url: str) -> tuple[bytes, datetime | None, str]: ...


class XeroFiler(Protocol):
    def attach_to_contact(
        self, contact_id: UUID, file_name: str, content: bytes, idempotency_key: str
    ) -> str: ...
    def add_contact_note(self, contact_id: UUID, details: str, idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class RunSettings:
    mailbox: str  # the mailbox the bureau alerts arrive in
    arr_file_url: str | None  # SharePoint web URL of the ARR file
    summary_to: str | None  # where the daily summary goes (None: print only)


@dataclass
class RunReport:
    arr: str = "not checked"
    alerts_read: int = 0
    alerts_unreadable: int = 0
    assessments: list[dict[str, Any]] = field(default_factory=list)
    filed: list[str] = field(default_factory=list)
    filing_errors: list[str] = field(default_factory=list)
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run(uow: UnitOfWork, settings: RunSettings, mail: Mailbox, xero: XeroFiler | None) -> RunReport:
    report = RunReport()
    _step(report, "ARR file", lambda: _arr(uow, settings, mail, report))
    _step(report, "alerts", lambda: _alerts(uow, settings, mail, report))
    _step(report, "assessments", lambda: _assess(uow, report))
    _step(report, "filing", lambda: _file(uow, settings, mail, xero, report))
    with uow() as conn:
        report.exceptions = credit.credit_exceptions(conn)
    if settings.summary_to:
        _step(
            report,
            "summary email",
            lambda: mail.send_mail(
                settings.mailbox, settings.summary_to or "", summary_subject(report), summary_html(report)
            ),
        )
    return report


def _step(report: RunReport, name: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:  # one failed step must not stop the others; reported, retried next run
        report.errors.append(f"{name}: {type(exc).__name__}: {exc}")


def _arr(uow: UnitOfWork, settings: RunSettings, mail: Mailbox, report: RunReport) -> None:
    if not settings.arr_file_url:
        report.arr = "no ARR file configured (SALES_ORDERS_ARR_FILE_URL)"
        return
    content, modified, name = mail.download_shared_file(settings.arr_file_url)
    parsed = parse_arr_file(content, name)
    with uow() as conn:
        snapshot_id, created = credit.load_arr_snapshot(conn, parsed, modified)
    report.arr = (
        f"new version loaded (snapshot {snapshot_id}, {len(parsed.lines)} lines"
        + (f", {len(parsed.skipped)} rows without an amount" if parsed.skipped else "")
        + ")"
        if created
        else f"unchanged (snapshot {snapshot_id})"
    )


def _alerts(uow: UnitOfWork, settings: RunSettings, mail: Mailbox, report: RunReport) -> None:
    with uow() as conn:
        last = credit.latest_alert_received(conn)
        senders = conn.execute(
            "SELECT bureau_code, alert_sender FROM sales.credit_bureau ORDER BY sort_order"
        ).fetchall()
    since = (last - OVERLAP) if last else datetime.now(UTC) - FIRST_RUN_LOOKBACK
    for s in senders:
        for m in mail.messages_from(settings.mailbox, s["alert_sender"], since):
            with uow() as conn:
                r = credit.store_alert(
                    conn,
                    bureau=s["bureau_code"],
                    internet_message_id=m.internet_message_id,
                    received_at=m.received_at,
                    subject=m.subject,
                    html=m.html,
                    mailbox_message_id=m.graph_id,
                )
            if r.created:
                report.alerts_read += 1
                report.alerts_unreadable += r.parse_error is not None


def _assess(uow: UnitOfWork, report: RunReport) -> None:
    with uow() as conn:
        report.assessments = credit.assess_due(conn)
        credit.queue_filing(conn)


def _file(
    uow: UnitOfWork, settings: RunSettings, mail: Mailbox, xero: XeroFiler | None, report: RunReport
) -> None:
    with uow() as conn:
        tasks = credit.pending_filing(conn)
    for t in tasks:
        ref: str | None = None
        error: str | None = None
        try:
            if t["task_code"] == "xero_snapshot":
                ref = _file_xero(uow, xero, t)
            else:
                if not t["debt_credit_folder_id"]:
                    raise RuntimeError("no Debt & Credit folder mapped")
                if not t["mailbox_message_id"]:
                    raise RuntimeError("the original email's mailbox id is not known")
                ref = mail.copy_message(settings.mailbox, t["mailbox_message_id"], t["debt_credit_folder_id"])
        except Exception as exc:  # recorded against the task and retried; never stops the run
            error = f"{type(exc).__name__}: {exc}"
        with uow() as conn:
            credit.record_filing(conn, int(t["credit_filing_task_id"]), ref, error)
        label = f"{t['task_code']} {t['display_name']}"
        (report.filing_errors if error else report.filed).append(f"{label}: {error}" if error else label)


def _file_xero(uow: UnitOfWork, xero: XeroFiler | None, t: dict[str, Any]) -> str:
    if xero is None:
        raise RuntimeError("Xero is not configured (SALES_ORDERS_XERO_CLIENT_ID / _SECRET)")
    if not t["xero_contact_id"]:
        raise RuntimeError("the customer has no Xero contact id")
    with uow() as conn:
        name, pdf = credit.ensure_snapshot(conn, int(t["credit_assessment_id"]))
        note = credit.xero_note(conn, int(t["credit_assessment_id"]))
    key = f"credit-assessment-{t['credit_assessment_id']}"
    attachment_id = xero.attach_to_contact(UUID(str(t["xero_contact_id"])), name, pdf, key)
    xero.add_contact_note(UUID(str(t["xero_contact_id"])), note, key + "-note")
    return attachment_id


# ---------------------------------------------------------------------------- summary


def summary_subject(report: RunReport) -> str:
    reviews = sum(1 for e in report.exceptions if e["rule_code"] == "CREDIT_REVIEW_NEEDED")
    changed = sum(1 for a in report.assessments if a["outcome_code"] == "applied")
    problems = len(report.errors) + len(report.filing_errors)
    return f"Credit run: {changed} limit(s) updated, {reviews} decision(s) needed" + (
        f", {problems} problem(s)" if problems else ""
    )


def _rows(items: Iterator[str] | list[str]) -> str:
    return "".join(f"<li>{html.escape(i)}</li>" for i in items) or "<li>None</li>"


def summary_html(report: RunReport) -> str:
    def money(v: Any) -> str:
        return f"£{v:,.0f}" if v is not None else "N/A"

    assessed = [
        f"{a['display_name']}: {a['outcome_code'].replace('_', ' ')} - requirement {money(a['trading_requirement'])}"
        f" (Experian {money(a['experian_limit'])}, Creditsafe {money(a['creditsafe_limit'])}, "
        f"trigger {a['trigger_code'].replace('_', ' ')})"
        + (f". Needs you: {a['review_reason']}" if a["review_reason"] else "")
        for a in report.assessments
    ]
    applied = [
        f"{a['display_name']}: {money(a['recommended_limit'])}"
        for a in report.assessments
        if a["outcome_code"] == "applied"
    ]
    errors = [e for e in report.exceptions if e["severity"] == "error"]
    warnings = [e for e in report.exceptions if e["severity"] == "warning"]
    return f"""
<p>Daily credit run. ARR file: {html.escape(report.arr)}. Bureau alerts read: {report.alerts_read}
({report.alerts_unreadable} unreadable).</p>
<h3>Assessments this run</h3><ul>{_rows(assessed)}</ul>
<h3>Credit limits to mirror in Xero (Xero's API cannot set them)</h3><ul>{_rows(applied)}</ul>
<h3>Needs you (errors)</h3><ul>{_rows([f"{e['display_name']}: {e['message']} - {e['action']}" for e in errors])}</ul>
<h3>For information (warnings)</h3><ul>{_rows([f"{e['display_name']}: {e['message']}" for e in warnings])}</ul>
<h3>Filed</h3><ul>{_rows(report.filed)}</ul>
<h3>Problems in this run</h3><ul>{_rows(report.errors + report.filing_errors)}</ul>
<p>To decide a limit: sales-orders credit-decide "&lt;company&gt;" &lt;limit&gt; --reason "...".</p>
"""


def file_xero_tasks(uow: UnitOfWork, xero: XeroFiler | None, report: RunReport) -> None:
    """Only the Xero filing tasks (the daily Claude job files emails itself, via the connector)."""
    with uow() as conn:
        tasks = [t for t in credit.pending_filing(conn) if t["task_code"] == "xero_snapshot"]
    for t in tasks:
        ref: str | None = None
        error: str | None = None
        try:
            ref = _file_xero(uow, xero, t)
        except Exception as exc:  # recorded against the task and retried; never stops the run
            error = f"{type(exc).__name__}: {exc}"
        with uow() as conn:
            credit.record_filing(conn, int(t["credit_filing_task_id"]), ref, error)
        label = f"xero_snapshot {t['display_name']}"
        (report.filing_errors if error else report.filed).append(f"{label}: {error}" if error else label)


def report_since(conn: Connection, since: datetime | None = None) -> RunReport:
    """What happened since `since` (default: start of today, UTC), for the summary email."""
    start = since or datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    report = RunReport()
    report.arr = "see the job log"
    alerts = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE parse_error IS NOT NULL) AS bad"
        " FROM sales.credit_alert_email WHERE loaded_at >= %s",
        (start,),
    ).fetchone()
    if alerts:
        report.alerts_read, report.alerts_unreadable = int(alerts["n"]), int(alerts["bad"])
    ids = [
        r["credit_assessment_id"]
        for r in conn.execute(
            "SELECT credit_assessment_id FROM sales.credit_assessment WHERE assessed_at >= %s ORDER BY 1",
            (start,),
        )
    ]
    report.assessments = [credit.assessment(conn, int(i)) for i in ids]
    for t in conn.execute(
        """SELECT t.task_code, s.display_name, t.status_code, t.last_error
             FROM sales.credit_filing_task t JOIN sales.credit_subject s USING (credit_subject_id)
            WHERE t.updated_at >= %s ORDER BY t.credit_filing_task_id""",
        (start,),
    ):
        label = f"{t['task_code']} {t['display_name']}"
        if t["status_code"] == "done":
            report.filed.append(label)
        elif t["last_error"]:
            report.filing_errors.append(f"{label}: {t['last_error']}")
    report.exceptions = credit.credit_exceptions(conn)
    return report
