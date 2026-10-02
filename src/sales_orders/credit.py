"""Credit & risk management services (migration 0014). Like service.py: the only code that writes
credit data, every function inside the caller's transaction.

The rules (exposure per frequency, VAT, rounding, risk appetite, when to reassess, what needs the
CFO) live in the database; this module moves data in and out and never recalculates a figure.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from sales_orders.credit_alerts import AlertCompany, AlertFormatError, parse_alert
from sales_orders.credit_arr import ParsedArrFile
from sales_orders.credit_pdf import SnapshotData, SnapshotLine, file_name, render
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError, UnknownReferenceError

LONDON = ZoneInfo("Europe/London")
MAX_FILING_ATTEMPTS = 3


# ============================================================================ bureau alerts


@dataclass(frozen=True)
class AlertResult:
    credit_alert_email_id: int
    created: bool  # False = this Message-ID was already loaded; nothing written
    companies: int
    parse_error: str | None


def bureau_for_sender(conn: Connection, sender: str) -> str | None:
    row = conn.execute(
        "SELECT bureau_code FROM sales.credit_bureau WHERE lower(alert_sender) = lower(%s)", (sender,)
    ).fetchone()
    return str(row["bureau_code"]) if row else None


def store_alert(
    conn: Connection,
    *,
    bureau: str,
    internet_message_id: str,
    received_at: datetime,
    subject: str,
    html: str,
    mailbox_message_id: str | None = None,
) -> AlertResult:
    """Record one bureau alert email and a reading per company in it. Idempotent on Message-ID.

    An email that cannot be read is still recorded (with the reason) so it is reported, not lost.
    """
    existing = conn.execute(
        "SELECT credit_alert_email_id, companies, parse_error FROM sales.credit_alert_email"
        " WHERE internet_message_id = %s",
        (internet_message_id,),
    ).fetchone()
    if existing:
        return AlertResult(
            int(existing["credit_alert_email_id"]), False, int(existing["companies"]), existing["parse_error"]
        )
    if received_at.tzinfo is None:
        raise SalesOrderError("received_at must include a timezone")
    try:
        companies: list[AlertCompany] = parse_alert(bureau, html)
        error = None
    except AlertFormatError as exc:
        companies, error = [], str(exc)
    row = conn.execute(
        """
        INSERT INTO sales.credit_alert_email (internet_message_id, mailbox_message_id, bureau_code,
                                              received_at, subject, body_sha256, parse_error, companies)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING credit_alert_email_id
        """,
        (
            internet_message_id,
            mailbox_message_id,
            bureau,
            received_at,
            subject,
            hashlib.sha256(html.encode()).hexdigest(),
            error,
            len(companies),
        ),
    ).fetchone()
    if row is None:
        raise SalesOrderError("internal error: alert email not recorded")
    email_id = int(row["credit_alert_email_id"])
    for c in companies:
        conn.execute(
            """
            INSERT INTO sales.credit_report
                (bureau_code, company_number, bureau_ref, company_name, observed_at, source_code,
                 credit_alert_email_id, limit_status, credit_limit, previous_credit_limit, credit_rating,
                 risk_score, risk_band, events)
            VALUES (%s, %s, %s, %s, %s, 'alert_email', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                bureau,
                c.company_number,
                c.bureau_ref,
                c.company_name,
                received_at,
                email_id,
                c.limit_status,
                c.credit_limit,
                c.previous_credit_limit,
                c.credit_rating,
                c.risk_score,
                c.risk_band,
                json.dumps(list(c.events)),
            ),
        )
    return AlertResult(email_id, True, len(companies), error)


def latest_alert_received(conn: Connection) -> datetime | None:
    row = conn.execute("SELECT max(received_at) AS t FROM sales.credit_alert_email").fetchone()
    return row["t"] if row else None


# ============================================================================ ARR file


def load_arr_snapshot(
    conn: Connection, parsed: ParsedArrFile, modified_at: datetime | None = None
) -> tuple[int, bool]:
    """Load an ARR file version. The same file (by SHA-256) is never loaded twice.

    Every frequency and status must be known to the rule tables; otherwise nothing is loaded.
    """
    existing = conn.execute(
        "SELECT credit_arr_snapshot_id FROM sales.credit_arr_snapshot WHERE source_sha256 = %s",
        (parsed.sha256,),
    ).fetchone()
    if existing:
        return int(existing["credit_arr_snapshot_id"]), False
    known_freq = {
        r["arr_frequency"] for r in conn.execute("SELECT arr_frequency FROM sales.credit_exposure_rule")
    }
    known_status = {r["arr_status"] for r in conn.execute("SELECT arr_status FROM sales.credit_arr_status")}
    unknown_freq = sorted({ln.frequency for ln in parsed.lines} - known_freq)
    unknown_status = sorted({ln.status for ln in parsed.lines} - known_status)
    if unknown_freq or unknown_status:
        raise UnknownReferenceError(
            "ARR frequency/status",
            "; ".join(
                [
                    f"frequencies {unknown_freq}" if unknown_freq else "",
                    f"statuses {unknown_status}" if unknown_status else "",
                ]
            ).strip("; "),
        )
    row = conn.execute(
        """
        INSERT INTO sales.credit_arr_snapshot (source_name, source_sha256, source_modified_at, line_count)
        VALUES (%s, %s, %s, %s) RETURNING credit_arr_snapshot_id
        """,
        (parsed.source_name, parsed.sha256, modified_at, len(parsed.lines)),
    ).fetchone()
    if row is None:
        raise SalesOrderError("internal error: ARR snapshot not recorded")
    snapshot_id = int(row["credit_arr_snapshot_id"])
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sales.credit_arr_line (credit_arr_snapshot_id, source_row, customer_name, internal_ref,
                                               arr_status, arr_frequency, annual_revenue)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    snapshot_id,
                    ln.source_row,
                    ln.customer_name,
                    ln.internal_ref,
                    ln.status,
                    ln.frequency,
                    ln.annual_revenue,
                )
                for ln in parsed.lines
            ],
        )
    return snapshot_id, True


# ============================================================================ workbook import (go-live, once)

_LABEL_EXPERIAN = "experian suggested credit limit"
_LABEL_CREDITSAFE = "creditsafe"
_LABEL_ONE_OFF = "one off & project spend"
_LABEL_LIMIT = ("total acceptable credit risk", "amount to grant")


@dataclass(frozen=True)
class WorkbookSheet:
    sheet: str
    experian: tuple[Decimal | None, date | None] | None  # (limit or None for N/A, date)
    creditsafe: tuple[Decimal | None, date | None] | None
    one_off: Decimal | None
    workbook_limit: Decimal | None


def _right_of(row: tuple[Any, ...], start: int) -> tuple[Any, Any]:
    """The first value and the first date to the right of a label."""
    value = when = None
    for v in row[start + 1 :]:
        if isinstance(v, datetime) and when is None:
            when = v
        elif v is not None and value is None and not isinstance(v, datetime):
            value = v
    return value, when


def _decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return Decimal(str(v)).quantize(Decimal("0.01"))
    return None


def parse_workbook(content: bytes) -> list[WorkbookSheet]:
    """Read the superseded Credit Limit Assessment Workings file (cached values, every sheet)."""
    wb = load_workbook(io.BytesIO(content), data_only=True)
    sheets: list[WorkbookSheet] = []
    for ws in wb.worksheets:
        found: dict[str, Any] = {}
        for row in ws.iter_rows(values_only=True):
            for j, v in enumerate(row):
                if not isinstance(v, str):
                    continue
                label = v.strip().lower()
                value, when = _right_of(row, j)
                if label == _LABEL_EXPERIAN and "experian" not in found:
                    found["experian"] = (_decimal(value), when.date() if when else None)
                elif label == _LABEL_CREDITSAFE and "creditsafe" not in found:
                    found["creditsafe"] = (_decimal(value), when.date() if when else None)
                elif label == _LABEL_ONE_OFF and "one_off" not in found:
                    found["one_off"] = _decimal(value)
                elif label in _LABEL_LIMIT and "limit" not in found:
                    found["limit"] = _decimal(value)
        sheets.append(
            WorkbookSheet(
                sheet=ws.title,
                experian=found.get("experian"),
                creditsafe=found.get("creditsafe"),
                one_off=found.get("one_off"),
                workbook_limit=found.get("limit"),
            )
        )
    return sheets


def import_workbook(conn: Connection, sheets: list[WorkbookSheet]) -> dict[str, Any]:
    """Seed bureau readings and one-off allowances from the workbook (subjects matched by sheet code).

    Readings are dated as in the workbook, so any later alert supersedes them. Sheets with no
    monitored subject are reported, never created.
    """
    loaded, unmatched = [], []
    for s in sheets:
        subj = conn.execute(
            "SELECT credit_subject_id, display_name FROM sales.credit_subject WHERE workbook_sheet = %s",
            (s.sheet,),
        ).fetchone()
        if subj is None:
            unmatched.append(s.sheet)
            continue
        sid = int(subj["credit_subject_id"])
        for bureau, reading in (("experian", s.experian), ("creditsafe", s.creditsafe)):
            if reading is None:
                continue
            limit, when = reading
            if when is None:
                continue  # an undated workbook figure cannot be placed in time: not imported
            conn.execute(
                """
                INSERT INTO sales.credit_report (bureau_code, credit_subject_id, company_name, observed_at,
                                                 source_code, limit_status, credit_limit, events)
                VALUES (%s, %s, %s, %s, 'workbook_import', %s, %s, %s)
                ON CONFLICT (credit_subject_id, bureau_code, observed_at) WHERE credit_subject_id IS NOT NULL
                DO NOTHING
                """,
                (
                    bureau,
                    sid,
                    subj["display_name"],
                    datetime.combine(when, time(), LONDON),
                    "value" if limit is not None else "not_available",
                    limit,
                    json.dumps([f"Imported from Credit Limit Assessment Workings, sheet {s.sheet}"]),
                ),
            )
        if s.one_off is not None:
            conn.execute(
                "UPDATE sales.credit_subject SET one_off_allowance = %s"
                " WHERE credit_subject_id = %s AND one_off_allowance IS DISTINCT FROM %s",
                (s.one_off, sid, s.one_off),
            )
        loaded.append({"sheet": s.sheet, "subject": subj["display_name"], "workbook_limit": s.workbook_limit})
    return {"loaded": loaded, "unmatched_sheets": unmatched}


# ============================================================================ assessments


def assess_due(conn: Connection) -> list[dict[str, Any]]:
    """Assess every subject whose inputs changed (bureau limit or band, ARR requirement, allowance)."""
    done = []
    for due in conn.execute(
        "SELECT credit_subject_id, display_name, trigger_code FROM sales.v_credit_assessment_due"
        " ORDER BY display_name"
    ).fetchall():
        done.append(_assess(conn, int(due["credit_subject_id"]), str(due["trigger_code"])))
    return done


def assess(conn: Connection, subject: str) -> dict[str, Any]:
    """Assess one subject now (manual trigger), whether or not anything changed."""
    return _assess(conn, _subject_id(conn, subject), "manual")


def _assess(conn: Connection, subject_id: int, trigger: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT sales.create_credit_assessment(%s, %s) AS id", (subject_id, trigger)
    ).fetchone()
    if row is None:
        raise SalesOrderError("internal error: assessment not created")
    return assessment(conn, int(row["id"]))


def assessment(conn: Connection, assessment_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT a.*, s.display_name, s.customer_id
          FROM sales.v_credit_assessment a JOIN sales.credit_subject s USING (credit_subject_id)
         WHERE a.credit_assessment_id = %s
        """,
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise SalesOrderError(f"credit assessment {assessment_id} not found")
    return dict(row)


def _subject_id(conn: Connection, subject: str) -> int:
    row = conn.execute(
        """
        SELECT credit_subject_id FROM sales.credit_subject
         WHERE lower(btrim(display_name)) = lower(btrim(%s)) OR company_number = upper(%s)
        """,
        (subject, subject),
    ).fetchone()
    if row is None:
        raise UnknownReferenceError("credit subject", subject)
    return int(row["credit_subject_id"])


def decide(
    conn: Connection,
    subject: str,
    credit_limit: Decimal,
    reason: str,
    review_by: date | None = None,
) -> int:
    """CFO decision on a customer's limit (needs approve_credit_terms). Linked to the latest assessment."""
    sid = _subject_id(conn, subject)
    row = conn.execute(
        """
        SELECT s.customer_id, (SELECT max(credit_assessment_id) FROM sales.credit_assessment a
                                WHERE a.credit_subject_id = s.credit_subject_id) AS assessment_id
          FROM sales.credit_subject s WHERE s.credit_subject_id = %s
        """,
        (sid,),
    ).fetchone()
    if row is None or row["customer_id"] is None:
        raise SalesOrderError(f"{subject} is not a customer: only customers have a credit limit")
    limit_row = conn.execute(
        "SELECT sales.set_credit_limit(%s, %s, 'cfo_decision', %s, %s, %s) AS id",
        (row["customer_id"], credit_limit, row["assessment_id"], reason, review_by),
    ).fetchone()
    if limit_row is None:
        raise SalesOrderError("internal error: credit limit not recorded")
    return int(limit_row["id"])


# ============================================================================ snapshots and filing


def _decision_text(conn: Connection, a: dict[str, Any]) -> tuple[Decimal | None, str]:
    limit = conn.execute(
        """
        SELECT l.credit_limit, l.source_code, l.reason, l.review_by, l.approved_at, e.full_name
          FROM sales.customer_credit_limit l LEFT JOIN sales.employee e ON e.employee_id = l.approved_by_employee_id
         WHERE l.credit_assessment_id = %s ORDER BY l.customer_credit_limit_id DESC LIMIT 1
        """,
        (a["credit_assessment_id"],),
    ).fetchone()
    if limit:
        if limit["source_code"] == "assessment":
            return limit[
                "credit_limit"
            ], "Applied automatically: the trading requirement is within risk appetite."
        review = f" Review by {limit['review_by']:%d %b %Y}." if limit["review_by"] else ""
        return (
            limit["credit_limit"],
            f"CFO decision ({limit['full_name']}, {limit['approved_at']:%d %b %Y}): {limit['reason']}.{review}"
            + (f" Assessment flagged: {a['review_reason']}." if a["review_reason"] else ""),
        )
    if a["customer_id"] is None:
        return None, "Not a customer: assessed for information; no credit limit is set."
    current = conn.execute(
        "SELECT credit_limit, reason, review_by FROM sales.v_customer_current_credit_limit WHERE customer_id = %s",
        (a["customer_id"],),
    ).fetchone()
    if a["outcome_code"] == "unchanged" and current:
        return current[
            "credit_limit"
        ], "Limit unchanged: the trading requirement is the same as the limit in force."
    if a["outcome_code"] == "override_in_force" and current:
        return (
            current["credit_limit"],
            f"CFO decision in force ({current['reason']}); this assessment recommends "
            f"£{a['recommended_limit']:,.0f}.",
        )
    return None, f"Awaiting CFO decision: {a['review_reason']}."


def snapshot_data(conn: Connection, assessment_id: int) -> SnapshotData:
    a = assessment(conn, assessment_id)
    lines = conn.execute(
        "SELECT arr_frequency, annual_revenue, exposure FROM sales.credit_assessment_line WHERE credit_assessment_id = %s",
        (assessment_id,),
    ).fetchall()
    arr = conn.execute(
        "SELECT source_name, loaded_at FROM sales.credit_arr_snapshot WHERE credit_arr_snapshot_id = %s",
        (a["credit_arr_snapshot_id"],),
    ).fetchone()
    limit, decision = _decision_text(conn, a)
    return SnapshotData(
        company_name=str(a["display_name"]),
        assessment_id=assessment_id,
        assessed_at=a["assessed_at"],
        experian_limit=a["experian_limit"],
        experian_date=a["experian_observed_at"],
        experian_band=a["experian_band"],
        creditsafe_limit=a["creditsafe_limit"],
        creditsafe_date=a["creditsafe_observed_at"],
        baseline=a["baseline"],
        lines=tuple(SnapshotLine(r["arr_frequency"], r["annual_revenue"], r["exposure"]) for r in lines),
        one_off_allowance=a["one_off_allowance"],
        net_requirement=a["net_requirement"],
        vat_rate=a["vat_rate"],
        vat=a["vat"],
        gross_requirement=a["gross_requirement"],
        trading_requirement=a["trading_requirement"],
        risk_appetite=a["risk_appetite"],
        appetite_pct=a["appetite_pct"],
        appetite_applies=bool(a["appetite_applies"]),
        credit_limit=limit,
        decision=decision,
        arr_source=f"{arr['source_name']} (loaded {arr['loaded_at']:%d %b %Y %H:%M})" if arr else None,
    )


def ensure_snapshot(conn: Connection, assessment_id: int) -> tuple[str, bytes]:
    """The filed PDF for an assessment: created once, then always the same bytes."""
    row = conn.execute(
        "SELECT file_name, pdf FROM sales.credit_assessment_snapshot WHERE credit_assessment_id = %s",
        (assessment_id,),
    ).fetchone()
    if row:
        return str(row["file_name"]), bytes(row["pdf"])
    data = snapshot_data(conn, assessment_id)
    name, pdf = file_name(data), render(data)
    conn.execute(
        "INSERT INTO sales.credit_assessment_snapshot (credit_assessment_id, file_name, pdf) VALUES (%s, %s, %s)",
        (assessment_id, name, pdf),
    )
    return name, pdf


def queue_filing(conn: Connection) -> int:
    row = conn.execute("SELECT sales.queue_credit_filing() AS n").fetchone()
    return int(row["n"]) if row else 0


def pending_filing(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT t.credit_filing_task_id, t.task_code, t.credit_assessment_id, t.attempts,
               s.display_name, s.debt_credit_folder_id, c.xero_contact_id,
               e.mailbox_message_id, e.internet_message_id
          FROM sales.credit_filing_task t
          JOIN sales.credit_subject s USING (credit_subject_id)
          LEFT JOIN sales.customer c ON c.customer_id = s.customer_id
          LEFT JOIN sales.credit_alert_email e USING (credit_alert_email_id)
         WHERE t.status_code = 'pending'
         ORDER BY t.credit_filing_task_id
        """
    ).fetchall()


def record_filing(conn: Connection, task_id: int, external_ref: str | None, error: str | None) -> None:
    """Mark a filing task done, or count a failed attempt (failed for good after MAX_FILING_ATTEMPTS)."""
    if error is None:
        conn.execute(
            """UPDATE sales.credit_filing_task
                  SET status_code = 'done', external_ref = %s, completed_at = now(), attempts = attempts + 1,
                      last_error = NULL
                WHERE credit_filing_task_id = %s""",
            (external_ref, task_id),
        )
    else:
        conn.execute(
            """UPDATE sales.credit_filing_task
                  SET attempts = attempts + 1, last_error = %s,
                      status_code = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
                WHERE credit_filing_task_id = %s""",
            (error[:2000], MAX_FILING_ATTEMPTS, task_id),
        )


def retry_failed_filing(conn: Connection) -> int:
    cur = conn.execute(
        "UPDATE sales.credit_filing_task SET status_code = 'pending', attempts = 0 WHERE status_code = 'failed'"
    )
    return cur.rowcount


def xero_note(conn: Connection, assessment_id: int) -> str:
    d = snapshot_data(conn, assessment_id)
    limit = f"£{d.credit_limit:,.0f}" if d.credit_limit is not None else "not set"
    return (
        f"Credit limit {limit} (assessment {assessment_id}, {d.assessed_at:%d %b %Y}). "
        f"Experian £{d.experian_limit or 0:,.0f}, Creditsafe £{d.creditsafe_limit or 0:,.0f}, "
        f"trading requirement £{d.trading_requirement:,.0f}. {d.decision}"
    )


# ============================================================================ Debt & Credit folders

_NOISE = re.compile(r"\b(limited|ltd|plc|llp|uk|group|holdings?|the)\b|[^a-z0-9]")


def _norm(name: str) -> str:
    return _NOISE.sub("", name.lower())


def map_folders(conn: Connection, folders: list[tuple[str, tuple[str, ...]]]) -> dict[str, Any]:
    """Match each client's "Debt & Credit" mail folder by its parent folder's name.

    folders: (folder id, path from the mailbox root). Only unmapped clients are changed, only on a
    single unambiguous match (normalised name equal to the subject's or customer's name).
    """
    candidates: dict[str, list[tuple[str, str]]] = {}
    for folder_id, path in folders:
        if len(path) >= 2 and path[-1].strip().lower() == "debt & credit":  # noqa: PLR2004 - parent + folder
            candidates.setdefault(_norm(path[-2]), []).append((folder_id, "/".join(path)))
    mapped, ambiguous, missing = [], [], []
    for s in conn.execute(
        """
        SELECT s.credit_subject_id, s.display_name, c.legal_name, c.trading_name
          FROM sales.credit_subject s
          JOIN sales.credit_relationship rel USING (relationship_code)
          LEFT JOIN sales.customer c USING (customer_id)
         WHERE rel.is_client AND s.is_active AND s.debt_credit_folder_id IS NULL
         ORDER BY s.display_name
        """
    ).fetchall():
        names = {_norm(n) for n in (s["display_name"], s["legal_name"], s["trading_name"]) if n}
        hits = {f for n in names for f in candidates.get(n, [])}
        if len(hits) == 1:
            folder_id, folder_path = next(iter(hits))
            conn.execute(
                "UPDATE sales.credit_subject SET debt_credit_folder_id = %s WHERE credit_subject_id = %s",
                (folder_id, s["credit_subject_id"]),
            )
            mapped.append({"subject": s["display_name"], "folder": folder_path})
        elif hits:
            ambiguous.append({"subject": s["display_name"], "folders": sorted(fp for _, fp in hits)})
        else:
            missing.append(s["display_name"])
    return {"mapped": mapped, "ambiguous": ambiguous, "missing": missing}


# ============================================================================ reporting


def credit_exceptions(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT e.rule_code, e.severity, e.display_name, e.message, r.action
          FROM sales.v_credit_exception e JOIN sales.credit_exception_rule r USING (rule_code)
         ORDER BY e.severity, e.rule_code, e.display_name
        """
    ).fetchall()


def credit_position(conn: Connection) -> list[dict[str, Any]]:
    """Every monitored company: relationship, bureau limits, latest assessment and limit in force."""
    return conn.execute(
        """
        SELECT i.display_name, i.relationship_code, i.experian_limit, i.creditsafe_limit, i.experian_band,
               a.credit_assessment_id, a.trading_requirement, a.outcome_code, a.assessed_at,
               l.credit_limit, l.source_code AS limit_source
          FROM sales.v_credit_subject_inputs i
          LEFT JOIN LATERAL (SELECT * FROM sales.v_credit_assessment va
                              WHERE va.credit_subject_id = i.credit_subject_id
                              ORDER BY va.credit_assessment_id DESC LIMIT 1) a ON true
          LEFT JOIN sales.v_customer_current_credit_limit l ON l.customer_id = i.customer_id
         ORDER BY i.relationship_code, i.display_name
        """
    ).fetchall()
