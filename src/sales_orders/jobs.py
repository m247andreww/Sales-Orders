"""The nightly sync: one scheduled run that brings every source into the database.

Each step runs in its own transaction and is logged in sales.job_run / sales.job_run_step, so a
failure in one source (say Xero is down) never undoes or blocks the others, and the run log says
exactly what happened. The run as a whole fails if any step fails, which is what raises the alert.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sales_orders import service, sync
from sales_orders.db import Connection
from sales_orders.google_sheets import SheetsClient, rows_as_csv
from sales_orders.reference import MOVES_TAB, STAFF_TAB, parse_moves, parse_staff
from sales_orders.register import parse_register
from sales_orders.xero import XeroClient, fetch_accounts, fetch_customers, read_contact_groups

Connect = Callable[[], AbstractContextManager[Connection]]
StepFunction = Callable[[Connection], str]

OK_MARKER = "NIGHTLY SYNC OK"  # the Azure alert watches for this line every day
FAILED_MARKER = "NIGHTLY SYNC FAILED"


@dataclass(frozen=True)
class Step:
    name: str
    run: StepFunction


@dataclass
class StepOutcome:
    name: str
    status: str  # succeeded | failed | skipped
    detail: str


@dataclass
class JobReport:
    run_id: int
    steps: list[StepOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)


def run_job(job_name: str, steps: Sequence[Step | StepOutcome], connect: Connect) -> JobReport:
    """Run steps in order; a StepOutcome in the list is recorded as given (e.g. 'skipped: not configured')."""
    with connect() as conn:
        row = service._required(
            service._one(
                conn, "INSERT INTO sales.job_run (job_name) VALUES (%s) RETURNING job_run_id", (job_name,)
            ),
            "job_run",
        )
    report = JobReport(run_id=int(row["job_run_id"]))
    for number, step in enumerate(steps, start=1):
        started = datetime.now(UTC)
        if isinstance(step, StepOutcome):
            outcome = step
        else:
            try:
                with connect() as conn:
                    outcome = StepOutcome(step.name, "succeeded", step.run(conn))
            except Exception as exc:  # recorded in the run log, then the next step runs
                last = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
                where = f" ({last.filename.rsplit('/', 1)[-1]}:{last.lineno})" if last else ""
                outcome = StepOutcome(step.name, "failed", f"{type(exc).__name__}: {exc}{where}")
        report.steps.append(outcome)
        with connect() as conn:
            conn.execute(
                """INSERT INTO sales.job_run_step (job_run_id, step_no, step_name, status, detail, started_at, finished_at)
                   VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())""",
                (report.run_id, number, outcome.name, outcome.status, outcome.detail[:4000], started),
            )
    with connect() as conn:
        failed = [s.name for s in report.steps if s.status == "failed"]
        conn.execute(
            "UPDATE sales.job_run SET status = %s, finished_at = clock_timestamp(), summary = %s WHERE job_run_id = %s",
            (
                "succeeded" if report.ok else "failed",
                "all steps succeeded" if report.ok else f"failed: {', '.join(failed)}",
                report.run_id,
            ),
        )
    return report


@dataclass(frozen=True)
class NightlyConfig:
    register_sheet_id: str
    register_range: str
    reference_sheet_id: str


def nightly_steps(
    cfg: NightlyConfig, sheets: SheetsClient, xero: XeroClient | None
) -> Iterator[Step | StepOutcome]:
    """Order matters: people and customers first, then the Register and the history built from it,
    then the account moves that act on that history, and last the Xero owner-group reconciliation."""

    def reference(conn: Connection) -> str:
        staff = parse_staff(sheets.values(cfg.reference_sheet_id, STAFF_TAB))
        return sync.load_staff(conn, staff)

    yield Step("reference-staff", reference)

    if xero is None:
        skipped = "Xero is not configured (SALES_ORDERS_XERO_CLIENT_ID / _SECRET)"
        yield StepOutcome("xero-chart-of-accounts", "skipped", skipped)
        yield StepOutcome("xero-customers", "skipped", skipped)
    else:
        client = xero
        yield Step(
            "xero-chart-of-accounts",
            lambda conn: sync.sync_xero_accounts(conn, fetch_accounts(client)).summary("GL accounts"),
        )
        yield Step(
            "xero-customers",
            lambda conn: sync.sync_xero_customers(conn, fetch_customers(client)).summary("customers"),
        )

    def register(conn: Connection) -> str:
        parsed = parse_register(rows_as_csv(sheets.values(cfg.register_sheet_id, cfg.register_range)))
        sync_id = service.load_register(
            conn, parsed, f"Google Sheets {cfg.register_sheet_id} / {cfg.register_range}"
        )
        assigned = service.assign_pending_sns(conn)
        return (
            f"Register sync {sync_id}: {len(parsed.rows)} rows loaded, {len(parsed.rejections)} rejected; "
            f"{len(assigned)} SN(s) assigned"
        )

    yield Step("register", register)

    def history(conn: Connection) -> str:
        r = service.build_account_history(conn, "AW SOs Register (nightly sync)")
        text = f"history built for {r['customers_built']} customer(s), {r['periods_created']} period(s)"
        if r["customers_skipped"]:
            labels = ", ".join(str(u["salesperson"]) for u in r["unmapped_labels"])
            text += f"; {r['customers_skipped']} skipped for unmapped Register labels ({labels}): add them to the Staff tab"
        return text

    yield Step("account-history", history)

    def moves(conn: Connection) -> str:
        return sync.apply_account_moves(
            conn, parse_moves(sheets.values(cfg.reference_sheet_id, f"'{MOVES_TAB}'"))
        )

    yield Step("account-moves", moves)

    if xero is None:
        yield StepOutcome("xero-owner-groups", "skipped", "Xero is not configured")
    else:
        group_client = xero

        def owner_groups(conn: Connection) -> str:
            groups, _raw = read_contact_groups(group_client)
            r = service.sync_xero_groups(conn, groups, "Xero API (nightly sync)")
            open_items = [d for d in r["differences"] if d["status"] != "MATCH"]
            return (
                f"Xero owner groups: {len(groups)} group(s), {r['xero_owner_changes']} change(s) seen, "
                f"{len(r['applied'])} applied, {len(open_items)} difference(s) to review (xero-owners)"
            )

        yield Step("xero-owner-groups", owner_groups)
