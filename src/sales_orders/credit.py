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
from uuid import UUID
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

from sales_orders.credit_alerts import AlertCompany, AlertFormatError, parse_alert
from sales_orders.credit_arr import ParsedArrFile
from sales_orders.credit_pdf import (
    AlertCompanyLines,
    AlertSnapshotData,
    SnapshotData,
    SnapshotLine,
    alert_file_name,
    file_name,
    render,
    render_alert,
)
from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError, UnknownReferenceError
from sales_orders.models import CreditSubjectIn, CustomerIn, MasterDataIn
from sales_orders.money import gbp
from sales_orders.service import load_master_data
from sales_orders.xero import SALES_TERMS_TYPES, XeroDirectoryContact

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


def _reading_json(r: tuple[Decimal | None, date | None] | None) -> list[str | None] | None:
    return (
        None if r is None else [str(r[0]) if r[0] is not None else None, r[1].isoformat() if r[1] else None]
    )


def _reading_from_json(v: Any) -> tuple[Decimal | None, date | None] | None:
    if v is None:
        return None
    limit, when = v
    return (Decimal(limit) if limit is not None else None, date.fromisoformat(when) if when else None)


def workbook_to_json(sheets: list[WorkbookSheet]) -> str:
    """The workbook figures as JSON (amounts as strings), so they can be carried without the file."""
    return json.dumps(
        [
            {
                "sheet": s.sheet,
                "experian": _reading_json(s.experian),
                "creditsafe": _reading_json(s.creditsafe),
                "one_off": str(s.one_off) if s.one_off is not None else None,
                "workbook_limit": str(s.workbook_limit) if s.workbook_limit is not None else None,
            }
            for s in sheets
        ],
        indent=1,
    )


def workbook_from_json(text: str) -> list[WorkbookSheet]:
    rows = json.loads(
        text, parse_float=lambda x: (_ for _ in ()).throw(ValueError("amounts must be strings"))
    )
    return [
        WorkbookSheet(
            sheet=str(r["sheet"]),
            experian=_reading_from_json(r["experian"]),
            creditsafe=_reading_from_json(r["creditsafe"]),
            one_off=Decimal(r["one_off"]) if r["one_off"] is not None else None,
            workbook_limit=Decimal(r["workbook_limit"]) if r["workbook_limit"] is not None else None,
        )
        for r in rows
    ]


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
    review_by: date,
    *,
    terms: PaymentTerms | None = None,
) -> int:
    """CFO decision on a customer's limit (needs approve_credit_terms). Linked to the latest assessment.

    A reason and a review / follow-up date after today are always required (CFO, 2026-10-05; migration 0022).
    """
    if not reason or not reason.strip():
        raise SalesOrderError("a credit decision needs a reason")
    if review_by is None or review_by <= datetime.now(LONDON).date():
        raise SalesOrderError("a credit decision needs a review / follow-up date after today")
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
    if terms is not None:  # decided with the limit, in the same transaction (CFO, 9 Oct 2026)
        set_payment_terms(conn, subject, terms, reason)
    return int(limit_row["id"])


def claim_desk_request(conn: Connection, request_id: str | None, kind: str, subject: str) -> bool:
    """Record a Credit Desk request as applied; False if it already was (migration 0024).

    Call inside the same transaction as the change, before making it: a job that finds the request already
    recorded skips it, so a button press is never applied twice by overlapping decision jobs.
    `request_id` None (a change made in a session, not from the page) is always applied.
    """
    if request_id is None:
        return True
    row = conn.execute(
        """INSERT INTO sales.credit_desk_request (request_id, kind_code, subject_text) VALUES (%s, %s, %s)
           ON CONFLICT (request_id) DO NOTHING RETURNING request_id""",
        (request_id, kind, subject),
    ).fetchone()
    return row is not None


BUREAUX = ("experian", "creditsafe")


@dataclass(frozen=True)
class BureauFigure:
    """One figure the CFO read from a bureau portal. `limit` None = the portal shows no limit ("N/A")."""

    limit: Decimal | None
    band: str | None = None


def enter_bureau_figures(conn: Connection, subject: str, figures: dict[str, BureauFigure]) -> list[int]:
    """Record the limits the CFO read from the Experian / Creditsafe portals (migration 0023).

    Used when a bureau has never alerted on a client, so the database has no figure to assess. Each figure is
    an ordinary dated bureau reading: the next alert supersedes it. Needs approve_credit_terms (held in the
    database). Returns the new reading ids.
    """
    if not figures:
        raise SalesOrderError("enter at least one bureau figure")
    for bureau, f in figures.items():
        if bureau not in BUREAUX:
            raise SalesOrderError(f"unknown bureau {bureau!r}: expected experian or creditsafe")
        if f.limit is not None and (
            not isinstance(f.limit, Decimal) or f.limit < 0 or not f.limit.is_finite()
        ):
            raise SalesOrderError(f"{bureau} limit must be a whole amount of money of 0 or more")
        if f.band is not None and bureau != "experian":
            raise SalesOrderError("only Experian has a risk band")
    sid = _subject_id(conn, subject)
    name = conn.execute(
        "SELECT display_name FROM sales.credit_subject WHERE credit_subject_id = %s", (sid,)
    ).fetchone()
    if name is None:
        raise SalesOrderError("internal error: credit subject vanished")
    observed = datetime.now(LONDON)
    ids = []
    for bureau, f in figures.items():
        row = conn.execute(
            """
            INSERT INTO sales.credit_report (bureau_code, credit_subject_id, company_name, observed_at,
                                             source_code, limit_status, credit_limit, risk_band, events)
            VALUES (%s, %s, %s, %s, 'cfo_entry', %s, %s, %s, %s)
            RETURNING credit_report_id
            """,
            (
                bureau,
                sid,
                name["display_name"],
                observed,
                "value" if f.limit is not None else "not_available",
                f.limit,
                f.band,
                json.dumps([f"Entered by the CFO from the {bureau.title()} portal on the Credit Desk"]),
            ),
        ).fetchone()
        if row is None:
            raise SalesOrderError("internal error: bureau figure not recorded")
        ids.append(int(row["credit_report_id"]))
    return ids


# Experian's own order, lowest risk first (the table holds the bands; this only orders them for the page).
_EXPERIAN_BAND_ORDER = (
    "Very Low Risk",
    "Low Risk",
    "Below Average Risk",
    "Above Average Risk",
    "High Risk",
    "Maximum Risk",
    "Serious Adverse Information",
)


def experian_bands(conn: Connection) -> list[str]:
    """The Experian bands the database accepts, in Experian's order (any new band last)."""
    bands = [
        str(r["band"])
        for r in conn.execute(
            "SELECT band FROM sales.credit_risk_band WHERE bureau_code = 'experian' ORDER BY band"
        )
    ]
    rank = {b: i for i, b in enumerate(_EXPERIAN_BAND_ORDER)}
    return sorted(bands, key=lambda b: rank.get(b, len(rank)))


def first_figures_needed(conn: Connection) -> list[dict[str, Any]]:
    """Active customers with no limit from Experian and/or Creditsafe yet, largest amount owed first."""
    out = []
    for r in conn.execute(
        """
        SELECT s.display_name, s.company_number, s.creditsafe_ref,
               e.credit_limit AS experian_limit, e.limit_report_id IS NOT NULL AS has_experian,
               c.credit_limit AS creditsafe_limit, c.limit_report_id IS NOT NULL AS has_creditsafe,
               x.outstanding, x.annual_recurring, l.credit_limit AS current_limit
          FROM sales.credit_subject s
          JOIN sales.customer cu ON cu.customer_id = s.customer_id
          LEFT JOIN sales.v_credit_bureau_position e
                 ON e.credit_subject_id = s.credit_subject_id AND e.bureau_code = 'experian'
          LEFT JOIN sales.v_credit_bureau_position c
                 ON c.credit_subject_id = s.credit_subject_id AND c.bureau_code = 'creditsafe'
          LEFT JOIN sales.v_credit_exposure x ON x.xero_contact_id = cu.xero_contact_id
          LEFT JOIN sales.v_customer_current_credit_limit l ON l.customer_id = s.customer_id
         WHERE s.is_active
           AND (e.limit_report_id IS NULL OR c.limit_report_id IS NULL)
         ORDER BY x.outstanding DESC NULLS LAST, s.display_name
        """
    ):
        out.append(
            {
                "company": r["display_name"],
                "company_number": r["company_number"],
                "creditsafe_ref": r["creditsafe_ref"],
                "needs": [
                    b
                    for b, has in (("experian", r["has_experian"]), ("creditsafe", r["has_creditsafe"]))
                    if not has
                ],
                "experian": _money_text(r["experian_limit"]) if r["has_experian"] else None,
                "creditsafe": _money_text(r["creditsafe_limit"]) if r["has_creditsafe"] else None,
                "owed": _money_text(r["outstanding"] or Decimal(0)),
                "annual_recurring": _money_text(r["annual_recurring"]),
                "current_limit": _money_text(r["current_limit"]),
            }
        )
    return out


PAYMENT_METHODS = ("direct_debit", "bank_transfer", "card")
MAX_TERMS_DAYS = 180  # as the register allows (0001)


@dataclass(frozen=True)
class PaymentTerms:
    """A customer's payment terms: days from the invoice date (CFO, 9 Oct 2026; migration 0026)."""

    recurring_days: int
    recurring_method: str
    one_off_days: int
    one_off_prepay: bool = False
    one_off_basis: str = "DAYSAFTERBILLDATE"  # Xero's kinds of payment terms (migration 0027)

    def validate(self) -> None:
        for label, days in (("recurring", self.recurring_days), ("one-off", self.one_off_days)):
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_TERMS_DAYS:
                raise SalesOrderError(f"{label} payment terms must be a whole number of days from 0 to 180")
        if self.one_off_basis not in SALES_TERMS_TYPES:
            raise SalesOrderError(f"unknown kind of payment terms {self.one_off_basis!r}")
        if self.recurring_method not in PAYMENT_METHODS:
            raise SalesOrderError(
                f"unknown payment method {self.recurring_method!r}: {', '.join(PAYMENT_METHODS)}"
            )


def _customer_of(conn: Connection, subject: str) -> int:
    row = conn.execute(
        "SELECT customer_id FROM sales.credit_subject WHERE credit_subject_id = %s",
        (_subject_id(conn, subject),),
    ).fetchone()
    if row is None or row["customer_id"] is None:
        raise SalesOrderError(f"{subject} is not a customer: only customers have payment terms")
    return int(row["customer_id"])


def set_payment_terms(
    conn: Connection, subject: str, terms: PaymentTerms, reason: str | None, source: str = "cfo"
) -> int:
    """Set a customer's payment terms from today (needs approve_credit_terms; migrations 0026-0027).

    Terms other than the standard (30 days, recurring and one-off, no prepayment) need a reason. `source` "cfo" is a
    CFO decision (written back to the Xero contact); "xero" is the daily copy of what Xero holds.
    """
    terms.validate()
    return _set_terms(conn, _customer_of(conn, subject), terms, reason, source)


def _set_terms(
    conn: Connection, customer_id: int, terms: PaymentTerms, reason: str | None, source: str
) -> int:
    row = conn.execute(
        "SELECT sales.set_customer_payment_terms(%s, %s, %s, %s, %s, %s, %s, %s) AS id",
        (
            customer_id,
            terms.recurring_days,
            terms.recurring_method,
            terms.one_off_days,
            terms.one_off_prepay,
            reason,
            terms.one_off_basis,
            source,
        ),
    ).fetchone()
    if row is None:
        raise SalesOrderError("internal error: payment terms not recorded")
    return int(row["id"])


def terms_text(r: dict[str, Any]) -> dict[str, Any]:
    """Plain-English terms for people: '30 days (Direct Debit)', '14 days', 'payment with order'."""
    method = {"direct_debit": "Direct Debit", "bank_transfer": "bank transfer", "card": "card"}
    rec = basis_text(int(r["recurring_terms_days"]), "DAYSAFTERBILLDATE")
    if r["recurring_payment_method_code"]:
        rec += f" ({method.get(str(r['recurring_payment_method_code']), r['recurring_payment_method_code'])})"
    one = (
        "payment with order"
        if r["one_off_prepayment_required"]
        else basis_text(
            int(r["one_off_terms_days"]), str(r.get("one_off_terms_basis") or "DAYSAFTERBILLDATE")
        )
    )
    return {
        "recurring": rec,
        "one_off": one,
        "standard": not r["is_non_standard"],
        "default": bool(r["is_default"]),
        "reason": r["reason"],
        "recurring_days": int(r["recurring_terms_days"]),
        "recurring_method": r["recurring_payment_method_code"],
        "one_off_days": int(r["one_off_terms_days"]),
        "one_off_prepay": bool(r["one_off_prepayment_required"]),
        "one_off_basis": str(r.get("one_off_terms_basis") or "DAYSAFTERBILLDATE"),
        "source": r.get("source_code"),
    }


def basis_text(day: int, basis: str) -> str:
    """Xero's four kinds of payment terms, in plain English."""
    if basis == "DAYSAFTERBILLDATE":
        return "due on invoice" if day == 0 else f"{day} days"
    if basis == "DAYSAFTERBILLMONTH":
        return f"{day} days after the end of the invoice month"
    teens = range(11, 14)
    suffix = "th" if day % 100 in teens else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    month = "invoice month" if basis == "OFCURRENTMONTH" else "following month"
    return f"by the {day}{suffix} of the {month}"


def with_one_off_terms(
    conn: Connection, subject: str, days: int, basis: str = "DAYSAFTERBILLDATE"
) -> PaymentTerms:
    """The customer's current terms with new one-off terms (recurring terms follow the invoices in Xero)."""
    t = customer_terms(conn, _customer_of(conn, subject))
    return PaymentTerms(
        recurring_days=int(t["recurring_days"]),
        recurring_method=str(t["recurring_method"] or "bank_transfer"),
        one_off_days=days,
        one_off_basis=basis,
    )


def customer_terms(conn: Connection, customer_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT recurring_terms_days, recurring_payment_method_code, one_off_terms_days, one_off_prepayment_required,
                  one_off_terms_basis, is_non_standard, reason, is_default, source_code
             FROM sales.v_customer_payment_terms WHERE customer_id = %s""",
        (customer_id,),
    ).fetchone()
    if row is None:
        raise SalesOrderError(f"customer {customer_id} not found")
    return terms_text(dict(row))


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
            f"{gbp(a['recommended_limit'])}.",
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


def alert_snapshot_data(conn: Connection, alert_id: int, subject_id: int) -> AlertSnapshotData:
    """One client's lines from one alert email, as read (the PDF filed on the Xero contact)."""
    e = conn.execute(
        """SELECT e.credit_alert_email_id, b.name AS bureau, b.alert_sender, e.subject, e.received_at,
                  e.internet_message_id, e.body_sha256, s.display_name
             FROM sales.credit_alert_email e
             JOIN sales.credit_bureau b USING (bureau_code)
             CROSS JOIN sales.credit_subject s
            WHERE e.credit_alert_email_id = %s AND s.credit_subject_id = %s""",
        (alert_id, subject_id),
    ).fetchone()
    if e is None:
        raise ValueError(f"alert {alert_id} / subject {subject_id} not found")
    rows = conn.execute(
        """SELECT company_name, company_number, bureau_ref, previous_credit_limit, credit_limit, limit_status,
                  credit_rating, risk_score, risk_band, events
             FROM sales.v_credit_report
            WHERE credit_alert_email_id = %s AND credit_subject_id = %s
            ORDER BY credit_report_id""",
        (alert_id, subject_id),
    ).fetchall()
    if not rows:
        raise ValueError(f"alert {alert_id} has no lines for subject {subject_id}")
    return AlertSnapshotData(
        client_name=str(e["display_name"]),
        alert_id=int(e["credit_alert_email_id"]),
        bureau=str(e["bureau"]),
        sender=str(e["alert_sender"]),
        subject=str(e["subject"]),
        received_at=e["received_at"],
        internet_message_id=str(e["internet_message_id"]),
        body_sha256=str(e["body_sha256"]),
        companies=tuple(
            AlertCompanyLines(
                company_name=str(r["company_name"]),
                company_number=r["company_number"],
                bureau_ref=r["bureau_ref"],
                previous_credit_limit=r["previous_credit_limit"],
                credit_limit=r["credit_limit"],
                limit_status=str(r["limit_status"]),
                credit_rating=r["credit_rating"],
                risk_score=r["risk_score"],
                risk_band=r["risk_band"],
                events=tuple(str(x) for x in r["events"]),
            )
            for r in rows
        ),
    )


def ensure_alert_snapshot(conn: Connection, alert_id: int, subject_id: int) -> tuple[str, bytes]:
    """The filed PDF for one client's part of one alert: created once, then always the same bytes."""
    row = conn.execute(
        """SELECT file_name, pdf FROM sales.credit_alert_snapshot
            WHERE credit_alert_email_id = %s AND credit_subject_id = %s""",
        (alert_id, subject_id),
    ).fetchone()
    if row:
        return str(row["file_name"]), bytes(row["pdf"])
    data = alert_snapshot_data(conn, alert_id, subject_id)
    name, pdf = alert_file_name(data), render_alert(data)
    conn.execute(
        """INSERT INTO sales.credit_alert_snapshot (credit_alert_email_id, credit_subject_id, file_name, pdf)
           VALUES (%s, %s, %s, %s)""",
        (alert_id, subject_id, name, pdf),
    )
    return name, pdf


def queue_filing(conn: Connection) -> int:
    row = conn.execute("SELECT sales.queue_credit_filing() AS n").fetchone()
    return int(row["n"]) if row else 0


def pending_filing(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT t.credit_filing_task_id, t.task_code, t.credit_assessment_id, t.attempts,
               t.credit_alert_email_id, t.credit_subject_id,
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

    return (
        f"Credit limit {gbp(d.credit_limit, 'not set')} (assessment {assessment_id}, {d.assessed_at:%d %b %Y}). "
        f"Experian {gbp(d.experian_limit, 'no limit reported')}, "
        f"Creditsafe {gbp(d.creditsafe_limit, 'no limit reported')}, "
        f"trading requirement {gbp(d.trading_requirement)}. {d.decision}"
    )


# ============================================================================ Debt & Credit folders

_NOISE = re.compile(r"\b(limited|ltd|plc|llp|uk|group|holdings?|the)\b|[^a-z0-9]")


def _norm(name: str) -> str:
    return _NOISE.sub("", name.lower())


# ============================================================================ new customers to monitor


def sync_xero_contacts(conn: Connection, contacts: list[XeroDirectoryContact]) -> int:
    """Refresh the copy of the Xero contact list (migration 0018). Returns how many contacts were new."""
    new = 0
    for c in contacts:
        row = conn.execute(
            """
            INSERT INTO sales.xero_contact_directory
                   (xero_contact_id, name, company_number, is_customer, is_supplier, contact_status,
                    sales_terms_days, sales_terms_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (xero_contact_id) DO UPDATE
               SET name = EXCLUDED.name, company_number = EXCLUDED.company_number,
                   is_customer = EXCLUDED.is_customer, is_supplier = EXCLUDED.is_supplier,
                   contact_status = EXCLUDED.contact_status, sales_terms_days = EXCLUDED.sales_terms_days,
                   sales_terms_type = EXCLUDED.sales_terms_type, last_synced_at = now()
            RETURNING (xmax = 0) AS inserted
            """,
            (
                c.contact_id,
                c.name,
                c.company_number,
                c.is_customer,
                c.is_supplier,
                c.status,
                c.sales_terms_days,
                c.sales_terms_type,
            ),
        ).fetchone()
        new += bool(row and row["inserted"])
    return new


def _xero_suggestion(conn: Connection, arr_name: str) -> dict[str, Any] | None:
    """The one active Xero customer whose name matches the ARR name (normalised), else None (never a guess)."""
    key = _norm(arr_name)
    if len(key) < MIN_NAME_KEY:
        return None
    rows = conn.execute(
        """SELECT xero_contact_id, name, company_number FROM sales.xero_contact_directory
            WHERE is_customer AND contact_status = 'ACTIVE' ORDER BY name"""
    ).fetchall()
    exact = [r for r in rows if _norm(r["name"]) == key]
    starts = [r for r in rows if _norm(r["name"]).startswith(key)]
    match = exact if len(exact) == 1 else starts if not exact and len(starts) == 1 else []
    if not match:
        return None
    r = match[0]
    number = r["company_number"]
    if number and number.isdigit() and len(number) < COMPANY_NUMBER_LENGTH:  # Xero often drops leading zeros
        number = number.zfill(COMPANY_NUMBER_LENGTH)
    return {"xero_contact_id": str(r["xero_contact_id"]), "xero_name": r["name"], "company_number": number}


MIN_NAME_KEY = 3
COMPANY_NUMBER_LENGTH = 8


def record_customer_match(
    conn: Connection,
    prefix: str,
    *,
    source: str,
    confidence: str,
    xero_contact_id: str | None = None,
    registered_name: str | None = None,
    company_number: str | None = None,
    note: str | None = None,
    excluded_reason: str | None = None,
) -> None:
    """Record (or replace) the researched Xero contact and registered company for an ARR prefix."""
    number = company_number.strip().upper() if company_number else None
    if number and number.isdigit():
        number = number.zfill(COMPANY_NUMBER_LENGTH)
    conn.execute(
        """
        INSERT INTO sales.credit_customer_match
               (arr_prefix, xero_contact_id, registered_name, company_number, source, confidence, note,
                excluded_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (arr_prefix) DO UPDATE
           SET xero_contact_id = EXCLUDED.xero_contact_id, registered_name = EXCLUDED.registered_name,
               company_number = EXCLUDED.company_number, source = EXCLUDED.source,
               confidence = EXCLUDED.confidence, note = EXCLUDED.note, excluded_reason = EXCLUDED.excluded_reason,
               written_to_xero_at = CASE WHEN sales.credit_customer_match.company_number
                                              IS NOT DISTINCT FROM EXCLUDED.company_number
                                         THEN sales.credit_customer_match.written_to_xero_at END
        """,
        (prefix, xero_contact_id, registered_name, number, source, confidence, note, excluded_reason),
    )


def matches_to_write_to_xero(conn: Connection) -> list[dict[str, Any]]:
    """Researched company numbers not yet on the Xero contact. Only "certain" ones: Xero is a source of truth."""
    return conn.execute(
        """SELECT m.arr_prefix, m.xero_contact_id, m.company_number, d.name
             FROM sales.credit_customer_match m JOIN sales.xero_contact_directory d USING (xero_contact_id)
            WHERE m.company_number IS NOT NULL AND m.written_to_xero_at IS NULL AND m.confidence = 'certain'
              AND d.company_number IS DISTINCT FROM m.company_number
            ORDER BY m.arr_prefix"""
    ).fetchall()


def mark_written_to_xero(conn: Connection, prefix: str) -> None:
    conn.execute(
        "UPDATE sales.credit_customer_match SET written_to_xero_at = now() WHERE arr_prefix = %s", (prefix,)
    )
    conn.execute(
        """UPDATE sales.xero_contact_directory d SET company_number = m.company_number
             FROM sales.credit_customer_match m
            WHERE m.arr_prefix = %s AND d.xero_contact_id = m.xero_contact_id""",
        (prefix,),
    )


def _match_suggestion(conn: Connection, prefix: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(suggestion, match details) from a researched match, if one is recorded."""
    m = conn.execute(
        """SELECT m.xero_contact_id, d.name AS xero_name, m.company_number, m.registered_name, m.source,
                  m.confidence, m.note
             FROM sales.credit_customer_match m
             LEFT JOIN sales.xero_contact_directory d USING (xero_contact_id)
            WHERE m.arr_prefix = %s""",
        (prefix,),
    ).fetchone()
    if m is None:
        return None, None
    suggestion = (
        {
            "xero_contact_id": str(m["xero_contact_id"]),
            "xero_name": m["xero_name"],
            "company_number": m["company_number"],
        }
        if m["xero_contact_id"]
        else None
    )
    details = {k: m[k] for k in ("registered_name", "company_number", "source", "confidence", "note")}
    return suggestion, details


def unmonitored_customers(conn: Connection) -> list[dict[str, Any]]:
    """Customers with commitments in the latest ARR file but no credit monitoring, largest first."""
    out = []
    for r in conn.execute(
        """SELECT u.arr_prefix, u.customer_name, u.annual_revenue, u.line_count, u.first_seen_at, u.is_new
             FROM sales.v_credit_unmonitored_customer u
             LEFT JOIN sales.credit_customer_match m USING (arr_prefix)
            WHERE m.excluded_reason IS NULL
            ORDER BY u.is_new DESC, u.annual_revenue DESC"""
    ):
        out.append(
            {
                "prefix": str(r["arr_prefix"]),
                "name": r["customer_name"],
                "annual_revenue": _money_text(r["annual_revenue"]),
                "lines": int(r["line_count"]),
                "first_seen": r["first_seen_at"].astimezone(LONDON).date().isoformat(),
                "new": bool(r["is_new"]),
                "xero": _xero_suggestion(conn, str(r["customer_name"]).split(" / ")[0]),
                "match": None,
            }
        )
        suggestion, details = _match_suggestion(conn, str(r["arr_prefix"]))
        if details:
            out[-1]["match"] = details
            if suggestion:
                out[-1]["xero"] = suggestion
            elif details["company_number"] and out[-1]["xero"] is None:
                out[-1]["xero"] = {
                    "xero_contact_id": None,
                    "xero_name": None,
                    "company_number": details["company_number"],
                }
    return out


def add_monitored_customer(
    conn: Connection, prefix: str, company_number: str, xero_contact_id: str | None = None
) -> str:
    """The CFO has added an unmonitored ARR customer to Experian/Creditsafe: create the client record.

    Only a prefix the scan lists can be added; the company number is required (it is how alerts are
    matched). The customer is the ARR file's name; an existing customer with that prefix is reused.
    Returns the client's display name.
    """
    row = conn.execute(
        "SELECT customer_name FROM sales.v_credit_unmonitored_customer WHERE arr_prefix = %s", (prefix,)
    ).fetchone()
    if row is None:
        raise SalesOrderError(f"ARR prefix {prefix} is not an unmonitored customer in the latest ARR file")
    number = company_number.strip().upper()
    if number.isdigit():
        number = number.zfill(COMPANY_NUMBER_LENGTH)
    contact = None
    if xero_contact_id:
        contact = conn.execute(
            "SELECT xero_contact_id FROM sales.xero_contact_directory WHERE xero_contact_id = %s",
            (xero_contact_id,),
        ).fetchone()
        if contact is None:
            raise SalesOrderError(f"Xero contact {xero_contact_id} is not in the Xero contact list")
    existing = conn.execute(
        """SELECT legal_name, trading_name, xero_tracking_customer, notes FROM sales.customer
            WHERE arr_prefix = %s""",
        (prefix,),
    ).fetchone()
    name = str(existing["legal_name"]) if existing else str(row["customer_name"]).split(" / ")[0]
    note = f"added to credit monitoring by the CFO on the Credit Desk ({datetime.now(LONDON):%d %b %Y})"
    customer = CustomerIn(
        legal_name=name,
        trading_name=existing["trading_name"] if existing else None,
        xero_tracking_customer=existing["xero_tracking_customer"] if existing else None,
        arr_prefix=prefix,
        company_number=number,
        xero_contact_id=UUID(xero_contact_id) if xero_contact_id else None,
        notes="; ".join(x for x in ((existing["notes"] if existing else None), note) if x),
    )
    subject = CreditSubjectIn(
        display_name=name,
        relationship="customer",
        customer_legal_name=name,
        company_number=number,
        notes=note,
    )
    load_master_data(conn, MasterDataIn(customers=(customer,), credit_subjects=(subject,)))
    return name


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


# ============================================================================ Credit Desk (the CFO's page)

_WHY = (
    ("exceeds the lower bureau limit", "Trading need is above the lower bureau limit"),
    ("no bureau credit limit", "No bureau limit to support the trading need"),
    ("exceeds risk appetite", "Trading need is above half the lower bureau limit"),
    ("risk band", "Experian risk band needs a look"),
)


def _plain_why(reason: str | None) -> str:
    text = (reason or "").lower()
    return next((plain for key, plain in _WHY if key in text), reason or "Needs a decision")


def _money_text(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def _review_position(conn: Connection, assessment_id: int) -> dict[str, Any]:
    """What a client waiting for a decision owes now and has in PandaDoc (latest daily snapshots)."""
    e = conn.execute(
        """SELECT x.current_amount, x.overdue_amount, x.overdue_over_60, x.outstanding, x.oldest_due_date,
                  x.pipeline_largest, x.pipeline_gross, c.xero_contact_id
             FROM sales.credit_assessment a
             JOIN sales.credit_subject s USING (credit_subject_id)
             JOIN sales.customer c ON c.customer_id = s.customer_id
             LEFT JOIN sales.v_credit_exposure x ON x.xero_contact_id = c.xero_contact_id
            WHERE a.credit_assessment_id = %s""",
        (assessment_id,),
    ).fetchone()
    if e is None or e["xero_contact_id"] is None:
        return {"owed": None, "pandadoc": []}
    docs = conn.execute(
        """SELECT name, status, grand_total, date_sent FROM sales.credit_pipeline_document
            WHERE xero_contact_id = %s AND credit_pipeline_snapshot_id =
                  (SELECT max(credit_pipeline_snapshot_id) FROM sales.credit_pipeline_snapshot)
            ORDER BY grand_total DESC""",
        (e["xero_contact_id"],),
    ).fetchall()
    return {
        "owed": {
            "current": _money_text(e["current_amount"] or Decimal(0)),
            "overdue": _money_text(e["overdue_amount"] or Decimal(0)),
            "overdue_over_60": _money_text(e["overdue_over_60"] or Decimal(0)),
            "total": _money_text(e["outstanding"] or Decimal(0)),
            "oldest_due": e["oldest_due_date"].isoformat() if e["oldest_due_date"] else None,
        },
        "pandadoc_largest_gross": _money_text(e["pipeline_gross"] or Decimal(0)),
        "pandadoc": [
            {
                "name": d["name"],
                "status": d["status"],
                "value": _money_text(d["grand_total"]),
                "sent": d["date_sent"].date().isoformat() if d["date_sent"] else None,
            }
            for d in docs
        ],
    }


def _exposure_export(conn: Connection) -> dict[str, Any]:
    from sales_orders import credit_exposure  # noqa: PLC0415 - avoids an import cycle

    money = (
        "annual_recurring",
        "credit_limit",
        "current_amount",
        "overdue_amount",
        "overdue_over_60",
        "outstanding",
        "pipeline_value",
        "pipeline_largest",
        "pipeline_gross",
        "exposure",
        "headroom",
        "exposure_if_signed",
        "headroom_if_signed",
    )
    rows = [
        {
            **{k: v for k, v in r.items() if k not in money and k != "oldest_due_date"},
            **{k: _money_text(r[k]) for k in money},
            "oldest_due_date": r["oldest_due_date"].isoformat() if r["oldest_due_date"] else None,
        }
        for r in credit_exposure.exposure_rows(conn)
    ]
    snaps = conn.execute(
        """SELECT (SELECT max(as_of) FROM sales.credit_receivable_snapshot) AS receivables_as_of,
                  (SELECT max(as_of) FROM sales.credit_pipeline_snapshot) AS pipeline_as_of"""
    ).fetchone()
    um = credit_exposure.latest_unmatched(conn)
    return {
        "exposure": rows,
        "exposure_as_of": {k: (v.isoformat() if v else None) for k, v in (snaps or {}).items()},
        "exposure_unmatched": {
            "receivables": [
                {"name": r["name"], "outstanding": _money_text(r["outstanding"])} for r in um["receivables"]
            ],
            "pipeline": [
                {
                    "name": r["name"],
                    "status": r["status"],
                    "value": _money_text(r["grand_total"]),
                    "client_company": r["client_company"],
                }
                for r in um["pipeline"]
            ],
        },
    }


def desk_export(conn: Connection, since: datetime) -> dict[str, Any]:
    """Everything the Credit Desk page shows, as plain JSON (amounts as strings, never floats).

    `review`: the latest assessment of each company still waiting for a CFO decision.
    `changed` / `unchanged`: assessments since `since` that applied a limit / kept it.
    """
    review = []
    for r in conn.execute(
        """
        SELECT a.credit_assessment_id, s.display_name, s.customer_id, a.review_reason, a.trading_requirement,
               a.experian_limit, a.creditsafe_limit, a.baseline, l.credit_limit AS current_limit
          FROM sales.v_credit_assessment a
          JOIN sales.credit_subject s USING (credit_subject_id)
          LEFT JOIN sales.v_customer_current_credit_limit l ON l.customer_id = s.customer_id
         WHERE a.credit_assessment_id IN (SELECT max(credit_assessment_id) FROM sales.credit_assessment
                                           GROUP BY credit_subject_id)
           AND a.outcome_code = 'cfo_review'
           AND NOT EXISTS (SELECT 1 FROM sales.customer_credit_limit x
                            WHERE x.credit_assessment_id = a.credit_assessment_id)
         ORDER BY a.trading_requirement DESC
        """
    ):
        review.append(
            {
                "assessment_id": int(r["credit_assessment_id"]),
                "company": r["display_name"],
                "why": _plain_why(r["review_reason"]),
                "detail": r["review_reason"],
                "trading_need": _money_text(r["trading_requirement"]),
                "experian": _money_text(r["experian_limit"]),
                "creditsafe": _money_text(r["creditsafe_limit"]),
                "half_lower": _money_text((r["baseline"] or Decimal(0)) / 2),
                "current_limit": _money_text(r["current_limit"]),
                "terms": customer_terms(conn, int(r["customer_id"])),
                **_review_position(conn, int(r["credit_assessment_id"])),
            }
        )
    changed: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for r in conn.execute(
        """
        SELECT s.display_name, a.outcome_code, a.recommended_limit,
               (SELECT p.credit_limit FROM sales.customer_credit_limit p
                 WHERE p.customer_id = s.customer_id AND p.effective_from < a.assessed_at
                 ORDER BY p.effective_from DESC LIMIT 1) AS previous_limit
          FROM sales.v_credit_assessment a JOIN sales.credit_subject s USING (credit_subject_id)
         WHERE a.assessed_at >= %s AND a.outcome_code IN ('applied', 'unchanged')
         ORDER BY s.display_name
        """,
        (since,),
    ):
        row = {
            "company": r["display_name"],
            "old": _money_text(r["previous_limit"]),
            "new": _money_text(r["recommended_limit"]),
        }
        (changed if r["outcome_code"] == "applied" else unchanged).append(row)
    alerts = [
        {
            "bureau": r["bureau_code"],
            "company": r["company_name"],
            "monitored": r["credit_subject_id"] is not None,
            "old": _money_text(r["previous_credit_limit"]),
            "new": _money_text(r["credit_limit"]),
            "limit_status": r["limit_status"],
            "band": r["risk_band"],
        }
        for r in conn.execute(
            """SELECT r.bureau_code, r.company_name, r.credit_subject_id, r.previous_credit_limit, r.credit_limit,
                      r.limit_status, r.risk_band
                 FROM sales.v_credit_report r JOIN sales.credit_alert_email e USING (credit_alert_email_id)
                WHERE e.loaded_at >= %s ORDER BY r.credit_subject_id IS NULL, r.company_name""",
            (since,),
        )
    ]
    exceptions = credit_exceptions(conn)
    first_figures = first_figures_needed(conn)
    needing = {f["company"] for f in first_figures}
    return {
        "run_at": datetime.now(LONDON).isoformat(timespec="minutes"),
        "review": review,
        "changed": changed,
        "unchanged": unchanged,
        "alerts": alerts,
        "attention": [
            {"rule": e["rule_code"], "company": e["display_name"], "message": e["message"]}
            for e in exceptions
            if e["rule_code"] not in ("CREDIT_REVIEW_NEEDED", "ARR_NOT_MONITORED")  # own list
            # a missing bureau figure for a client in "first figures" is already asked for there
            and not (e["rule_code"] in ("SINGLE_BUREAU", "NO_BUREAU_LIMIT") and e["display_name"] in needing)
        ],
        "unmonitored": unmonitored_customers(conn),
        "first_figures": first_figures,
        "risk_bands": experian_bands(conn),
        **_exposure_export(conn),
        "unmonitored_excluded": [
            {"prefix": str(r["arr_prefix"]), "name": r["customer_name"], "reason": r["excluded_reason"]}
            for r in conn.execute(
                """SELECT u.arr_prefix, u.customer_name, m.excluded_reason
                     FROM sales.v_credit_unmonitored_customer u JOIN sales.credit_customer_match m USING (arr_prefix)
                    WHERE m.excluded_reason IS NOT NULL ORDER BY u.customer_name"""
            )
        ],
    }


def register_export(conn: Connection) -> dict[str, Any]:
    """The internal Customer Credit Register: limit, payment terms and what is owed, per customer.

    For colleagues (sales, credit control): no bureau figures and no decision reasons, which stay on the Credit Desk.
    """
    rows = []
    for r in conn.execute(
        """
        SELECT s.display_name, c.customer_id, l.credit_limit, l.review_by,
               x.current_amount, x.overdue_amount, x.outstanding, x.oldest_due_date,
               t.recurring_terms_days, t.recurring_payment_method_code, t.one_off_terms_days,
               t.one_off_prepayment_required, t.one_off_terms_basis, t.is_non_standard, t.reason, t.is_default,
               t.source_code
          FROM sales.credit_subject s
          JOIN sales.customer c ON c.customer_id = s.customer_id
          JOIN sales.v_customer_payment_terms t ON t.customer_id = c.customer_id
          LEFT JOIN sales.v_customer_current_credit_limit l ON l.customer_id = c.customer_id
          LEFT JOIN sales.v_credit_exposure x ON x.xero_contact_id = c.xero_contact_id
         WHERE s.is_active
         ORDER BY lower(s.display_name)
        """
    ):
        owed = r["outstanding"] or Decimal(0)
        overdue = r["overdue_amount"] or Decimal(0)
        limit = r["credit_limit"]
        if limit is None:
            status = "no_limit"
        elif owed > limit:
            status = "over_limit"
        elif overdue > 0:
            status = "overdue"
        else:
            status = "ok"
        terms = terms_text(dict(r))
        terms.pop("reason")  # decision reasons stay on the Credit Desk
        rows.append(
            {
                "customer": r["display_name"],
                "credit_limit": _money_text(limit),
                "review_by": r["review_by"].isoformat() if r["review_by"] else None,
                "terms": terms,
                "not_yet_due": _money_text(r["current_amount"] or Decimal(0)),
                "overdue": _money_text(overdue),
                "owed": _money_text(owed),
                "oldest_due": r["oldest_due_date"].isoformat() if r["oldest_due_date"] else None,
                "status": status,
            }
        )
    std = conn.execute(
        "SELECT numeric_value::integer AS d FROM sales.policy_setting WHERE setting_key = 'credit_standard_terms_days'"
    ).fetchone()
    asof = conn.execute("SELECT max(as_of) AS d FROM sales.credit_receivable_snapshot").fetchone()
    return {
        "run_at": datetime.now(LONDON).isoformat(timespec="minutes"),
        "standard_terms_days": int(std["d"]) if std else None,
        "owed_as_of": asof["d"].isoformat() if asof and asof["d"] else None,
        "customers": rows,
        "not_credit_checked": [
            {"name": u["name"], "annual_revenue": u["annual_revenue"]} for u in unmonitored_customers(conn)
        ],
    }


# ============================================================================ payment terms from Xero (0027)


def load_invoices(conn: Connection, doc: dict[str, Any]) -> tuple[int, list[str]]:
    """Load the recent sales invoices read from Xero (number, contact, invoice and due dates).

    `doc` = {"as_of", "source", "invoices": [{"invoice_number", "xero_contact_id", "invoice_date", "due_date"}]}.
    Invoices for contacts not in the Xero contact list are reported, never guessed. The same file loads once.
    """
    invoices = doc.get("invoices")
    if not isinstance(invoices, list) or not doc.get("as_of") or not doc.get("source"):
        raise SalesOrderError("the invoices file needs as_of, source and an invoices list")
    sha = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
    if conn.execute(
        "SELECT 1 FROM sales.credit_invoice_snapshot WHERE source_sha256 = %s", (sha,)
    ).fetchone():
        raise SalesOrderError("this invoices file has already been loaded")
    row = conn.execute(
        """INSERT INTO sales.credit_invoice_snapshot (as_of, source, source_sha256) VALUES (%s, %s, %s)
           RETURNING credit_invoice_snapshot_id""",
        (date.fromisoformat(str(doc["as_of"])), str(doc["source"]), sha),
    ).fetchone()
    if row is None:
        raise SalesOrderError("internal error: invoices snapshot not created")
    snap = int(row["credit_invoice_snapshot_id"])
    known = {
        str(r["xero_contact_id"])
        for r in conn.execute("SELECT xero_contact_id FROM sales.xero_contact_directory")
    }
    unmatched = []
    for inv in invoices:
        if not isinstance(inv, dict) or not all(
            inv.get(k) for k in ("invoice_number", "xero_contact_id", "invoice_date", "due_date")
        ):
            raise SalesOrderError(f"invoice without number, contact or dates: {inv!r}")
        if str(inv["xero_contact_id"]) not in known:
            unmatched.append(str(inv["invoice_number"]))
            continue
        conn.execute(
            """INSERT INTO sales.credit_invoice_line
                   (credit_invoice_snapshot_id, invoice_number, xero_contact_id, invoice_date, due_date)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (
                snap,
                str(inv["invoice_number"]),
                str(inv["xero_contact_id"]),
                date.fromisoformat(str(inv["invoice_date"])[:10]),
                date.fromisoformat(str(inv["due_date"])[:10]),
            ),
        )
    return snap, unmatched


def _xero_terms_reason(x: dict[str, Any]) -> str:
    one = basis_text(int(x["one_off_days"]), str(x["one_off_basis"]))
    bits = [f"one-off {one}" + (" (Xero contact)" if x["sales_terms_days"] is not None else " (standard)")]
    if x["recurring_days"] is not None:
        how = "RD invoices, collected by Direct Debit" if x["collected_by_dd"] else "RI invoices"
        bits.append(f"monthly {x['recurring_days']} days ({how})")
    return "From Xero: " + "; ".join(bits)


def sync_terms_from_xero(conn: Connection) -> list[dict[str, Any]]:
    """Make each credited customer's terms match Xero (CFO, 9 Oct 2026: "refer to xero"; migration 0027).

    One-off terms = the Xero contact's sales terms (else the standard). Recurring = what the latest invoices show:
    RD invoices (Direct Debit) first, else RI; no recurring invoice seen keeps the recurring terms on record.
    A CFO change not yet written to Xero (write `terms_to_write_to_xero` first) is never overwritten.
    """
    changes = []
    for x in conn.execute(
        """SELECT x.customer_id, s.display_name, x.sales_terms_days, x.sales_terms_type, x.collected_by_dd,
                  x.recurring_days, x.recurring_method, x.one_off_days, x.one_off_basis, x.standard_terms_days,
                  t.recurring_terms_days, t.recurring_payment_method_code, t.one_off_terms_days,
                  t.one_off_terms_basis, t.one_off_prepayment_required, t.source_code, t.is_default
             FROM sales.v_xero_customer_terms x
             JOIN sales.credit_subject s ON s.customer_id = x.customer_id AND s.is_active
             JOIN sales.v_customer_payment_terms t ON t.customer_id = x.customer_id
            ORDER BY s.display_name"""
    ).fetchall():
        cfo_pending = (
            x["source_code"] == "cfo"
            and not x["is_default"]
            and (x["one_off_terms_days"], x["one_off_terms_basis"]) != (x["one_off_days"], x["one_off_basis"])
        )
        nothing_in_xero = (
            x["is_default"]
            and x["recurring_days"] is None
            and (x["one_off_days"], x["one_off_basis"]) == (x["standard_terms_days"], "DAYSAFTERBILLDATE")
        )
        if cfo_pending or nothing_in_xero:  # standard terms need no register entry
            continue
        want = PaymentTerms(
            recurring_days=int(
                x["recurring_days"] if x["recurring_days"] is not None else x["recurring_terms_days"]
            ),
            recurring_method=str(
                x["recurring_method"] or x["recurring_payment_method_code"] or "bank_transfer"
            ),
            one_off_days=int(x["one_off_days"]),
            one_off_basis=str(x["one_off_basis"]),
        )
        have = (
            int(x["recurring_terms_days"]),
            x["recurring_payment_method_code"],
            int(x["one_off_terms_days"]),
            str(x["one_off_terms_basis"]),
            bool(x["one_off_prepayment_required"]),
        )
        if have == (want.recurring_days, want.recurring_method, want.one_off_days, want.one_off_basis, False):
            continue
        reason = _xero_terms_reason(dict(x))
        _set_terms(conn, int(x["customer_id"]), want, reason, "xero")
        changes.append({"customer": x["display_name"], "terms": reason})
    return changes


def terms_to_write_to_xero(conn: Connection) -> list[dict[str, Any]]:
    """CFO-set one-off terms that the Xero contact does not show yet (write these before syncing from Xero)."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT s.display_name, c.xero_contact_id, t.one_off_terms_days AS days, t.one_off_terms_basis AS kind
                 FROM sales.v_customer_payment_terms t
                 JOIN sales.customer c USING (customer_id)
                 JOIN sales.credit_subject s ON s.customer_id = c.customer_id AND s.is_active
                 JOIN sales.xero_contact_directory d ON d.xero_contact_id = c.xero_contact_id
                WHERE t.source_code = 'cfo' AND NOT t.is_default AND NOT t.one_off_prepayment_required
                  AND (d.sales_terms_days, d.sales_terms_type)
                      IS DISTINCT FROM (t.one_off_terms_days, t.one_off_terms_basis)
                ORDER BY s.display_name"""
        )
    ]


def mark_terms_written(conn: Connection, xero_contact_id: UUID, days: int, kind: str) -> None:
    """Record that the Xero contact now carries these terms (after Xero confirmed the write)."""
    conn.execute(
        "UPDATE sales.xero_contact_directory SET sales_terms_days = %s, sales_terms_type = %s"
        " WHERE xero_contact_id = %s",
        (days, kind, xero_contact_id),
    )
