"""Command-line interface: `sales-orders --help`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import psycopg
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ValidationError

from sales_orders import credit, credit_run, service, state_store
from sales_orders.config import database_url
from sales_orders.credit_arr import ArrFileError, parse_arr_extract, parse_arr_file
from sales_orders.db import unit_of_work
from sales_orders.errors import SalesOrderError
from sales_orders.graph import GraphClient, GraphCredentials, GraphError
from sales_orders.models import EmployeeAbsenceIn, MasterDataIn, OrderSubmissionIn
from sales_orders.register import parse_register
from sales_orders.state_store import BlobStateStore, StateStoreError
from sales_orders.xero import (
    CREDIT_SCOPE,
    DEFAULT_SCOPE,
    XeroClient,
    XeroCredentials,
    XeroFormatError,
    fetch_contact_groups,
    parse_contact_groups,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
M = TypeVar("M", bound=BaseModel)


def _actor(args: argparse.Namespace) -> str:
    return str(args.actor or os.environ.get("SALES_ORDERS_ACTOR") or getpass.getuser())


def _read_model(path: str, model: type[M]) -> M:
    # Unquoted JSON numbers with a decimal point become floats here; the models reject
    # floats for money, so an unquoted amount fails validation instead of being rounded.
    return model.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def cmd_migrate(_args: argparse.Namespace) -> int:
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    print("database is at the latest schema version")
    return 0


def cmd_load_master_data(args: argparse.Namespace) -> int:
    data = _read_model(args.file, MasterDataIn)
    with unit_of_work(_actor(args)) as conn:
        service.load_master_data(conn, data)
    print(
        f"master data loaded: {len(data.employees)} employees, {len(data.suppliers)} suppliers, "
        f"{len(data.customers)} customers, {len(data.fx_rates)} FX rates"
    )
    return 0


def cmd_load_order(args: argparse.Namespace) -> int:
    submission = _read_model(args.file, OrderSubmissionIn)
    with unit_of_work(_actor(args)) as conn:
        result = service.create_sales_order(conn, submission)
    verb = "created" if result.created else "already loaded (no change)"
    print(f"{result.order_number} {verb}")
    return cmd_show(argparse.Namespace(order_number=result.order_number, actor=args.actor))


def cmd_show(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        s = service.order_summary(conn, args.order_number)
        lines = service.order_lines(conn, args.order_number)
        checks = service.order_checks(conn, args.order_number)
        exceptions = service.order_exceptions(conn, args.order_number)
    ccy = s["currency_code"]
    m = service.money
    print(f"\n{s['order_number']}  {s['title']}")
    print(f"SN: {s['sn_ref'] or 'NOT YET ASSIGNED (see AW SOs Register)'}   Customer: {s['customer_name']}")
    print(
        f"Status: {s['status_code']}   Type: {s['order_type_code']}   Reporting: {s['reporting_category_code'] or '-'}"
    )
    print(
        f"{'#':>2} {'Description':<44} {'Qty':>7} {'Per':>3} {'Cost':>12} {'Sell':>12} {'GM':>11} {'GM%':>6}"
    )
    for ln in lines:
        print(
            f"{ln['line_number']:>2} {ln['description'][:44]:<44} {ln['quantity']:>7.2f} "
            f"{ln['billing_periods']:>3} {m(ln['net_cost'], ccy):>12} {m(ln['net_sell'], ccy):>12} "
            f"{m(ln['gross_margin'], ccy):>11} {_pct(ln['gross_margin_pct']):>6}  "
            f"{ln['revenue_gl_code'] or '?'}/{ln['cost_gl_code'] or '?'}  {ln['arr_ref'] or ''}"
        )
    print(
        f"{'':>2} {'TOTAL':<44} {'':>7} {'':>3} {m(s['net_cost'], ccy):>12} {m(s['net_sell'], ccy):>12} "
        f"{m(s['gross_margin'], ccy):>11} {_pct(s['gross_margin_pct']):>6}"
    )
    print(
        f"One-off sell {m(s['one_off_sell'], ccy)} | Recurring contract sell "
        f"{m(s['recurring_contract_sell'], ccy)} | MRR {m(s['monthly_recurring_sell'], ccy)} "
        f"(margin {m(s['monthly_recurring_margin'], ccy)}/month)"
    )
    print("\nChecks:")
    for c in checks:
        print(f"  [{c['check_status_code']:<14}] {c['check_type_code']}: {c['notes'] or ''}")
    print("\nExceptions:" + ("  none" if not exceptions else ""))
    for e in exceptions:
        print(f"  {e['severity'].upper():<7} {e['rule_code']}: {e['message']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        service.change_status(conn, args.order_number, args.to_status, args.reason)
    print(f"{args.order_number} -> {args.to_status}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        service.record_check(
            conn,
            args.order_number,
            check_type=args.check_type,
            status=args.status,
            notes=args.notes,
            evidence_sha256=args.evidence,
        )
    print(f"{args.order_number}: {args.check_type} = {args.status}")
    return 0


def cmd_load_register(args: argparse.Namespace) -> int:
    with Path(args.file).open(encoding="utf-8", newline="") as fh:
        parsed = parse_register(fh)
    with unit_of_work(_actor(args)) as conn:
        sync_id = service.load_register(conn, parsed, args.source)
        assigned = service.assign_pending_sns(conn)
    print(f"Register sync {sync_id}: {len(parsed.rows)} rows loaded, {len(parsed.rejections)} rejected")
    for r in parsed.rejections:
        print(f"  rejected row {r.row_number} ({r.raw_sn!r}): {r.reason}")
    for order_number, sn in assigned.items():
        print(f"  {order_number} -> {sn}")
    return 0


def cmd_assign_sn(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        sn = service.assign_sn(conn, args.order_number, args.sn)
        candidates = [] if sn else service.sn_candidates(conn, args.order_number)
    if sn:
        print(f"{args.order_number} -> {sn}")
        return 0
    print(f"{args.order_number}: no unique Register match. Candidates:")
    for c in candidates:
        print(
            f"  {c['sn_ref']} score {c['score']}: {c['client']} | {c['project']} | {c['date_issued']} | {c['revenue']}"
        )
    return 1


def cmd_arr(args: argparse.Namespace) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    with unit_of_work(_actor(args)) as conn:
        rows = service.arr_position(conn, as_of)
    total = sum((r["arr"] for r in rows), start=Decimal(0))
    for r in rows:
        print(f"{r['arr_ref']:<12} MRR {service.money(r['mrr']):>12}  ARR {service.money(r['arr']):>14}")
    print(f"{'TOTAL':<12} {'':>16}  ARR {service.money(total):>14}  (as of {as_of})")
    return 0


def cmd_link_arr(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        service.link_arr_ref(conn, args.order_number, args.line_number, args.arr_ref)
    print(f"{args.order_number} line {args.line_number} -> {args.arr_ref}")
    return 0


def cmd_arr_outstanding(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        rows = service.arr_refs_outstanding(conn)
    if not rows:
        print("No processed recurring lines are missing an ARR ref.")
        return 0
    total = sum((r["mrr_not_in_arr"] for r in rows), start=Decimal(0))
    print(
        f"ERROR: {len(rows)} processed recurring line(s) have no ARR ref; {service.money(total)} MRR missing from ARR"
    )
    for r in rows:
        print(
            f"  {r['sn_ref'] or r['order_number']} line {r['line_number']} {r['customer_name']}: {r['description'][:40]}"
            f" | MRR {service.money(r['mrr_not_in_arr'])} | {r['days_outstanding']} day(s) since approval"
        )
    return 1  # non-zero so a scheduled run flags it


def cmd_salesperson_history(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        rows = service.register_salesperson_history(conn, args.client)
    print("PROPOSAL ONLY: first/last order each salesperson handled, from the AW SOs Register.")
    print("Confirm allocation dates before loading them as account_allocations master data.")
    for r in rows:
        print(
            f"  {r['client']:<30} {r['salesperson'] or '-':<22} {r['first_order']} .. {r['last_order']}  ({r['orders']} orders)"
        )
    return 0


def cmd_employee_leaves(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        moved = service.employee_leaves(conn, args.email, date.fromisoformat(args.last_day))
    print(f"{args.email} deactivated; {moved} account(s) moved to House from the day after {args.last_day}")
    return 0


def cmd_allocate_account(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        service.allocate_account(
            conn, args.customer, args.email, date.fromisoformat(args.from_date), args.source
        )
    print(f"{args.customer} allocated to {args.email} from {args.from_date}")
    return 0


def _xero_credentials() -> XeroCredentials:
    client_id = os.environ.get("SALES_ORDERS_XERO_CLIENT_ID")
    secret = os.environ.get("SALES_ORDERS_XERO_CLIENT_SECRET")
    if not client_id or not secret:
        raise XeroFormatError(
            "set SALES_ORDERS_XERO_CLIENT_ID and SALES_ORDERS_XERO_CLIENT_SECRET (Xero custom connection), "
            "or pass --file with a saved ContactGroups response"
        )
    return XeroCredentials(
        client_id=client_id,
        client_secret=secret,
        scope=os.environ.get("SALES_ORDERS_XERO_SCOPE", DEFAULT_SCOPE),
        tenant_id=os.environ.get("SALES_ORDERS_XERO_TENANT_ID") or None,
    )


def _print_owner_differences(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("Xero owner groups and the database agree for every customer.")
        return
    print("Account owners (database vs Xero):")
    for r in rows:
        print(
            f"  [{r['status']}] {r['legal_name']}: database {r['db_owner'] or 'none'}, "
            f"Xero {r['xero_owner'] or r['xero_groups'] or 'none'} -> {r['action'] or 'no action'}"
        )


def cmd_sync_xero_groups(args: argparse.Namespace) -> int:
    if args.file:
        groups = parse_contact_groups(json.loads(Path(args.file).read_text(encoding="utf-8")))
        source = f"file {Path(args.file).name}"
    else:
        groups, _raw = fetch_contact_groups(_xero_credentials())
        source = "Xero API"
    with unit_of_work(_actor(args)) as conn:
        r = service.sync_xero_groups(conn, groups, source, adopt_xero=args.adopt_xero)
    print(
        f"Xero sync {r['sync_id']}: {len(groups)} group(s), {r['xero_owner_changes']} Xero owner change(s) seen."
    )
    for a in r["applied"]:
        print(f"  {a['customer']}: owner set to {a['xero_owner']} ({a['result']})")
    if r["unmapped_groups"]:
        print("Xero groups not mapped as owner groups (ignored): " + ", ".join(r["unmapped_groups"]))
    for u in r["unmatched_contacts"]:
        print(f"  Xero contact '{u['contact_name']}' ({u['group_name']}) is not a customer in this database")
    _print_owner_differences(r["differences"])
    return 0


def cmd_xero_owners(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        _print_owner_differences(service.account_owner_reconciliation(conn, include_matches=args.all))
    return 0


def cmd_build_account_history(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        r = service.build_account_history(conn, args.source)
    print(
        f"Account history built for {r['customers_built']} customer(s): {r['periods_created']} ownership period(s)."
    )
    if r["customers_skipped"]:
        print(
            f"{r['customers_skipped']} customer(s) skipped: Register salesperson labels with no owner mapping:"
        )
        for u in r["unmapped_labels"]:
            print(f"  '{u['salesperson']}' ({u['orders']} orders, {u['first_seen']}..{u['last_seen']})")
    if r["decisions"]:
        print("For CFO review: decisions the build had to make:")
        for d in r["decisions"]:
            print(f"  {d['customer']}: {d['note']}")
    if r["credit_after_leaving"]:
        print(
            "For CFO review: Register orders credited to a salesperson after their last day (credit kept, no ownership):"
        )
        for x in r["credit_after_leaving"]:
            print(
                f"  {x['sn_ref']} {x['date_issued']} {x['client']}: {x['salesperson']} (last day {x['left_on']})"
            )
    if r["conflicts"]:
        print("For CFO review: accounts where named salespeople alternate:")
        for c in r["conflicts"]:
            print(f"  {c['customer']}: back to {c['returns_to']} from {c['allocated_from']}")
    return 0


def cmd_record_absence(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        service.record_absence(
            conn,
            EmployeeAbsenceIn(
                email=args.email,
                absent_from=date.fromisoformat(args.absent_from),
                absent_to=date.fromisoformat(args.absent_to),
                reason=args.reason,
                source=args.source,
            ),
        )
    print(
        f"{args.email} absent {args.absent_from} to {args.absent_to} ({args.reason}): orders led by a colleague "
        "for their accounts in that period are treated as cover"
    )
    return 0


# ============================================================================ credit & risk


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SalesOrderError(f"{name} is not set (see docs/owner-guides/credit-automation.md)")
    return value


def _graph() -> GraphClient:
    return GraphClient(
        GraphCredentials(
            tenant_id=_env("SALES_ORDERS_GRAPH_TENANT_ID"),
            client_id=_env("SALES_ORDERS_GRAPH_CLIENT_ID"),
            client_secret=_env("SALES_ORDERS_GRAPH_CLIENT_SECRET"),
        )
    )


def _xero_credit_client() -> XeroClient | None:
    client_id = os.environ.get("SALES_ORDERS_XERO_CLIENT_ID")
    secret = os.environ.get("SALES_ORDERS_XERO_CLIENT_SECRET")
    if not client_id or not secret:
        return None  # filing to Xero is retried once the connection is configured
    return XeroClient(
        XeroCredentials(
            client_id=client_id,
            client_secret=secret,
            scope=os.environ.get("SALES_ORDERS_XERO_CREDIT_SCOPE", CREDIT_SCOPE),
            tenant_id=os.environ.get("SALES_ORDERS_XERO_TENANT_ID") or None,
        )
    )


def _gbp(value: Any) -> str:
    return f"£{value:,.0f}" if value is not None else "-"


def cmd_credit_run(args: argparse.Namespace) -> int:
    settings = credit_run.RunSettings(
        mailbox=_env("SALES_ORDERS_CREDIT_MAILBOX"),
        arr_file_url=os.environ.get("SALES_ORDERS_ARR_FILE_URL") or None,
        summary_to=None if args.no_email else (os.environ.get("SALES_ORDERS_CREDIT_SUMMARY_TO") or None),
    )
    actor = _actor(args)
    report = credit_run.run(lambda: unit_of_work(actor), settings, _graph(), _xero_credit_client())
    print(f"ARR file: {report.arr}")
    print(f"Bureau alerts read: {report.alerts_read} ({report.alerts_unreadable} unreadable)")
    for a in report.assessments:
        print(
            f"  {a['display_name']}: {a['outcome_code']} - requirement {_gbp(a['trading_requirement'])}"
            + (f" - {a['review_reason']}" if a["review_reason"] else "")
        )
    for f in report.filed:
        print(f"  filed: {f}")
    for e in report.errors + report.filing_errors:
        print(f"  PROBLEM: {e}")
    _print_credit_exceptions(report.exceptions)
    return 1 if report.errors else 0


def cmd_credit_load_arr(args: argparse.Namespace) -> int:
    if args.extract:
        parsed, modified = (
            parse_arr_extract(Path(args.extract).read_text(encoding="utf-8"), "ARR Live.xlsx"),
            None,
        )
    elif args.file:
        modified = None
        parsed = parse_arr_file(Path(args.file).read_bytes(), Path(args.file).name)
    else:
        content, modified, name = _graph().download_shared_file(_env("SALES_ORDERS_ARR_FILE_URL"))
        parsed = parse_arr_file(content, name)
    with unit_of_work(_actor(args)) as conn:
        snapshot_id, created = credit.load_arr_snapshot(conn, parsed, modified)
    print(
        f"ARR snapshot {snapshot_id}: {'loaded' if created else 'already loaded (same file)'}, {len(parsed.lines)} lines"
    )
    for row, reason in parsed.skipped:
        print(f"  row {row} not loaded: {reason}")
    return 0


def cmd_credit_read_alert(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        r = credit.store_alert(
            conn,
            bureau=args.bureau,
            internet_message_id=args.message_id,
            received_at=datetime.fromisoformat(args.received),
            subject=args.subject,
            html=Path(args.file).read_text(encoding="utf-8"),
            mailbox_message_id=args.mailbox_id,
        )
    state = "loaded" if r.created else "already loaded"
    print(
        f"alert {r.credit_alert_email_id} {state}: {r.companies} compan(ies)"
        + (f"; NOT READ: {r.parse_error}" if r.parse_error else "")
    )
    return 0 if r.parse_error is None else 1


def cmd_credit_alerts_known(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        rows = conn.execute(
            """SELECT internet_message_id FROM sales.credit_alert_email
                WHERE received_at >= now() - make_interval(days => %s) ORDER BY received_at""",
            (args.days,),
        ).fetchall()
        latest = credit.latest_alert_received(conn)
    print(
        json.dumps(
            {
                "latest_received": latest.isoformat() if latest else None,
                "known_message_ids": [r["internet_message_id"] for r in rows],
            },
            indent=1,
        )
    )
    return 0


def cmd_credit_pending_mail(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        tasks = [t for t in credit.pending_filing(conn) if t["task_code"] == "mail_copy"]
    print(
        json.dumps(
            [
                {
                    "task_id": t["credit_filing_task_id"],
                    "client": t["display_name"],
                    "folder_id": t["debt_credit_folder_id"],
                    "mailbox_message_id": t["mailbox_message_id"],
                    "internet_message_id": t["internet_message_id"],
                }
                for t in tasks
            ],
            indent=1,
        )
    )
    return 0


def cmd_credit_mark_filed(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        credit.record_filing(conn, args.task_id, args.ref, args.error)
    print(f"task {args.task_id}: {'attempt failed: ' + args.error if args.error else 'done'}")
    return 0


def cmd_credit_file_xero(args: argparse.Namespace) -> int:
    xero = _xero_credit_client()
    actor = _actor(args)
    report = credit_run.RunReport()
    credit_run.file_xero_tasks(lambda: unit_of_work(actor), xero, report)
    for f in report.filed:
        print(f"  filed: {f}")
    for e in report.filing_errors:
        print(f"  PROBLEM: {e}")
    return 1 if report.filing_errors else 0


def cmd_credit_summary(args: argparse.Namespace) -> int:
    since = datetime.fromisoformat(args.since) if args.since else None
    with unit_of_work(_actor(args)) as conn:
        report = credit_run.report_since(conn, since)
    Path(args.out).write_text(credit_run.summary_html(report), encoding="utf-8")
    print(credit_run.summary_subject(report))
    return 0


def cmd_credit_desk_export(args: argparse.Namespace) -> int:
    since = datetime.fromisoformat(args.since) if args.since else datetime.now(UTC) - timedelta(hours=20)
    with unit_of_work(_actor(args)) as conn:
        desk = credit.desk_export(conn, since)
    if args.run_note:
        desk["run_note"] = args.run_note
    Path(args.out).write_text(json.dumps(desk, indent=1), encoding="utf-8")
    print(
        f"desk: {len(desk['review'])} to decide, {len(desk['changed'])} changed, {len(desk['alerts'])} alert lines"
    )
    return 0


def _state_store() -> BlobStateStore:
    return BlobStateStore(_env("SALES_ORDERS_STATE_URL"))


def cmd_state_restore(_args: argparse.Namespace) -> int:
    print(state_store.restore(_state_store(), database_url()))
    return 0


def cmd_state_save(_args: argparse.Namespace) -> int:
    print(state_store.save(_state_store(), database_url()))
    return 0


def cmd_credit_import_workbook(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if path.suffix.lower() == ".json":
        sheets = credit.workbook_from_json(path.read_text(encoding="utf-8"))
    else:
        sheets = credit.parse_workbook(path.read_bytes())
    if args.export_json:
        Path(args.export_json).write_text(credit.workbook_to_json(sheets), encoding="utf-8")
        print(f"wrote {len(sheets)} sheet(s) to {args.export_json}; nothing imported")
        return 0
    with unit_of_work(_actor(args)) as conn:
        r = credit.import_workbook(conn, sheets)
    print(f"Imported {len(r['loaded'])} sheet(s).")
    if r["unmatched_sheets"]:
        print(
            "Sheets with no monitored company (add workbook_sheet to its credit subject): "
            + ", ".join(r["unmatched_sheets"])
        )
    return 0


def cmd_credit_assess(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        done = [credit.assess(conn, args.subject)] if args.subject else credit.assess_due(conn)
        credit.queue_filing(conn)
    for a in done:
        print(
            f"{a['display_name']}: assessment {a['credit_assessment_id']} {a['outcome_code']}; "
            f"requirement {_gbp(a['trading_requirement'])}, baseline {_gbp(a['baseline'])}"
            + (f"; needs CFO: {a['review_reason']}" if a["review_reason"] else "")
        )
    if not done:
        print("Nothing to assess: no inputs have changed.")
    return 0


def cmd_credit_decide(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        credit.decide(
            conn,
            args.subject,
            Decimal(args.limit),
            args.reason,
            date.fromisoformat(args.review_by) if args.review_by else None,
        )
        credit.queue_filing(conn)
    print(
        f"{args.subject}: credit limit set to £{Decimal(args.limit):,.0f}; snapshot filed to Xero on the next run"
    )
    return 0


def _print_credit_exceptions(rows: list[dict[str, Any]]) -> None:
    print("\nCredit exceptions:" + ("  none" if not rows else ""))
    for e in rows:
        print(f"  {e['severity'].upper():<7} {e['rule_code']}: {e['display_name']} - {e['message']}")
        print(f"          -> {e['action']}")


def cmd_credit_status(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        position = credit.credit_position(conn)
        exceptions = credit.credit_exceptions(conn)
    print(
        f"{'Company':<34} {'Type':<11} {'Experian':>11} {'Creditsafe':>11} {'Requirement':>12} {'Limit':>11}  Outcome"
    )
    for p in position:
        print(
            f"{p['display_name'][:34]:<34} {p['relationship_code']:<11} {_gbp(p['experian_limit']):>11} "
            f"{_gbp(p['creditsafe_limit']):>11} {_gbp(p['trading_requirement']):>12} {_gbp(p['credit_limit']):>11}  "
            f"{p['outcome_code'] or 'not assessed'}"
            + (" (limit is a CFO decision)" if p["limit_source"] == "cfo_decision" else "")
        )
    _print_credit_exceptions(exceptions)
    return 0


def cmd_credit_snapshot(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        name, pdf = credit.ensure_snapshot(conn, args.assessment_id)
    out = Path(args.out) if args.out else Path(name)
    out.write_bytes(pdf)
    print(f"written {out}")
    return 0


def cmd_credit_map_folders(args: argparse.Namespace) -> int:
    mailbox = _env("SALES_ORDERS_CREDIT_MAILBOX")
    folders = [(f.folder_id, f.path) for f in _graph().folders(mailbox)]
    with unit_of_work(_actor(args)) as conn:
        r = credit.map_folders(conn, folders)
    for m in r["mapped"]:
        print(f"  mapped {m['subject']} -> {m['folder']}")
    for a in r["ambiguous"]:
        print(f"  AMBIGUOUS {a['subject']}: {', '.join(a['folders'])}")
    for name in r["missing"]:
        print(f"  no Debt & Credit folder found for {name}")
    return 0


def cmd_credit_retry_filing(args: argparse.Namespace) -> int:
    with unit_of_work(_actor(args)) as conn:
        n = credit.retry_failed_filing(conn)
    print(f"{n} failed filing task(s) will be retried on the next credit-run")
    return 0


def _add_credit_job_commands(sub: Any) -> None:
    """Steps the daily Claude job calls between Microsoft 365 connector actions."""
    p = sub.add_parser(
        "credit-alerts-known", help="JSON: alert Message-IDs already read, and the latest received time"
    )
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_credit_alerts_known)

    p = sub.add_parser(
        "credit-pending-mail", help="JSON: alert emails still to be filed in Debt & Credit folders"
    )
    p.set_defaults(func=cmd_credit_pending_mail)

    p = sub.add_parser("credit-mark-filed", help="record that a filing task was done (or failed)")
    p.add_argument("task_id", type=int)
    p.add_argument("--ref", help="id of the filed copy")
    p.add_argument("--error", help="why it failed (counts as an attempt)")
    p.set_defaults(func=cmd_credit_mark_filed)

    p = sub.add_parser(
        "credit-file-xero", help="file pending snapshots + notes on Xero contacts (Xero custom connection)"
    )
    p.set_defaults(func=cmd_credit_file_xero)

    p = sub.add_parser("credit-summary", help="write today's summary email (HTML)")
    p.add_argument("--out", required=True)
    p.add_argument("--since", help="ISO time; default: start of today (UTC)")
    p.set_defaults(func=cmd_credit_summary)

    p = sub.add_parser("credit-desk-export", help="write the Credit Desk page's data (JSON) for publishing")
    p.add_argument("--out", required=True)
    p.add_argument("--since", help="ISO time; default: the last 20 hours")
    p.add_argument(
        "--run-note", help="one plain sentence shown at the top of the page (e.g. a problem in this run)"
    )
    p.set_defaults(func=cmd_credit_desk_export)

    p = sub.add_parser(
        "state-restore", help="daily job: restore the saved database (Azure Blob) into an empty database"
    )
    p.set_defaults(func=cmd_state_restore)

    p = sub.add_parser("state-save", help="daily job: save the database (refuses if another run saved first)")
    p.set_defaults(func=cmd_state_save)


def _add_credit_commands(sub: Any) -> None:
    """Credit & risk management (migration 0014)."""
    p = sub.add_parser(
        "credit-run", help="daily routine: ARR file, bureau alerts, assessments, Xero and folder filing"
    )
    p.add_argument("--no-email", action="store_true", help="print the summary only")
    p.set_defaults(func=cmd_credit_run)

    p = sub.add_parser("credit-load-arr", help="load the ARR file (SharePoint, or --file)")
    p.add_argument("--file", help="a local copy of the workbook instead of SharePoint")
    p.add_argument(
        "--extract", help="the Microsoft 365 connector's read of the workbook (Credit Extract sheet)"
    )
    p.set_defaults(func=cmd_credit_load_arr)

    p = sub.add_parser("credit-read-alert", help="load one saved bureau alert email body (HTML)")
    p.add_argument("bureau", choices=["experian", "creditsafe"])
    p.add_argument("file")
    p.add_argument("--message-id", required=True, help="the email's Message-ID (idempotency key)")
    p.add_argument("--received", required=True, help="ISO time with offset, e.g. 2026-10-01T08:03:20+00:00")
    p.add_argument("--subject", default="bureau alert")
    p.add_argument(
        "--mailbox-id", help="the message's Outlook id (from the Microsoft 365 connector), for filing"
    )
    p.set_defaults(func=cmd_credit_read_alert)

    p = sub.add_parser(
        "credit-import-workbook", help="go-live: seed bureau limits and allowances from the workbook"
    )
    p.add_argument("file", help="the workbook (.xlsx) or its figures exported as .json")
    p.add_argument(
        "--export-json", help="write the workbook's figures to this .json file instead of importing"
    )
    p.set_defaults(func=cmd_credit_import_workbook)

    p = sub.add_parser("credit-assess", help="assess companies whose inputs changed (or one, now)")
    p.add_argument("subject", nargs="?", help="company name or number; omit for every company due")
    p.set_defaults(func=cmd_credit_assess)

    p = sub.add_parser("credit-decide", help="CFO: set a customer's credit limit (with reason)")
    p.add_argument("subject", help="company name or number")
    p.add_argument("limit", help="e.g. 100000")
    p.add_argument("--reason", required=True)
    p.add_argument("--review-by", help="YYYY-MM-DD; reported when passed")
    p.set_defaults(func=cmd_credit_decide)

    p = sub.add_parser("credit-status", help="every monitored company, its limits, and credit exceptions")
    p.set_defaults(func=cmd_credit_status)

    p = sub.add_parser("credit-snapshot", help="write an assessment's summary PDF")
    p.add_argument("assessment_id", type=int)
    p.add_argument("--out")
    p.set_defaults(func=cmd_credit_snapshot)

    p = sub.add_parser("credit-map-folders", help="find each client's Debt & Credit mail folder")
    p.set_defaults(func=cmd_credit_map_folders)

    p = sub.add_parser("credit-retry-filing", help="retry Xero / folder filing that failed")
    p.set_defaults(func=cmd_credit_retry_filing)


def _pct(value: Any) -> str:
    return "-" if value is None else f"{value:.1f}"


def _add_order_commands(sub: Any) -> None:
    """Orders: load, show, workflow, checks."""
    p = sub.add_parser("load-order", help="record an order submission (idempotent)")
    p.add_argument("file")
    p.set_defaults(func=cmd_load_order)

    p = sub.add_parser("show", help="show an order with totals, checks and exceptions")
    p.add_argument("order_number")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("status", help="move an order to a new status")
    p.add_argument("order_number")
    p.add_argument("to_status")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", help="record the outcome of a pre-processing check")
    p.add_argument("order_number")
    p.add_argument("check_type")
    p.add_argument("status", choices=["passed", "failed", "waived", "not_applicable", "pending"])
    p.add_argument("--notes")
    p.add_argument("--evidence", help="sha256 of a supporting document already recorded")
    p.set_defaults(func=cmd_check)


def _add_master_data_commands(sub: Any) -> None:
    """Master data, AW SOs Register / SN, account ownership."""
    p = sub.add_parser("load-master-data", help="load employees, suppliers, customers, FX rates")
    p.add_argument("file")
    p.set_defaults(func=cmd_load_master_data)

    p = sub.add_parser("load-register", help="load an AW SOs Register CSV export and source pending SNs")
    p.add_argument("file")
    p.add_argument("--source", required=True, help="where the export came from, e.g. the Google Sheet id")
    p.set_defaults(func=cmd_load_register)

    p = sub.add_parser("assign-sn", help="source an order's SN from the Register (auto, or --sn to choose)")
    p.add_argument("order_number")
    p.add_argument("--sn", help="SN chosen by a person, e.g. SN260533 (must be on the Register)")
    p.set_defaults(func=cmd_assign_sn)

    p = sub.add_parser("salesperson-history", help="propose account-allocation history from the Register")
    p.add_argument("--client", help="one customer (name as on the Register)")
    p.set_defaults(func=cmd_salesperson_history)

    p = sub.add_parser(
        "allocate-account", help="allocate a customer account (e.g. from House) to a salesperson"
    )
    p.add_argument("customer", help="customer legal name")
    p.add_argument("email", help="salesperson email")
    p.add_argument("from_date", help="YYYY-MM-DD")
    p.add_argument("--source", default="CFO", help="authority for the allocation")
    p.set_defaults(func=cmd_allocate_account)

    p = sub.add_parser(
        "build-account-history", help="derive account ownership from the Register (unfilled customers)"
    )
    p.add_argument("--source", default="AW SOs Register (built at CFO instruction 2026-09-24)")
    p.set_defaults(func=cmd_build_account_history)

    p = sub.add_parser("record-absence", help="record a salesperson's absence (colleague orders = cover)")
    p.add_argument("email")
    p.add_argument("absent_from", help="YYYY-MM-DD")
    p.add_argument("absent_to", help="YYYY-MM-DD")
    p.add_argument("--reason", default="holiday")
    p.add_argument("--source", default="CFO", help="where the absence is recorded (HR, Outlook calendar...)")
    p.set_defaults(func=cmd_record_absence)

    p = sub.add_parser("sync-xero-groups", help="read account owners from Xero contact groups and reconcile")
    p.add_argument("--file", help="a saved Xero ContactGroups response instead of the live API")
    p.add_argument(
        "--adopt-xero",
        action="store_true",
        help="CFO instruction: take Xero's owner where nothing shows which side is newer (first sync)",
    )
    p.set_defaults(func=cmd_sync_xero_groups)

    p = sub.add_parser("xero-owners", help="account owner: database vs Xero groups, with actions")
    p.add_argument("--all", action="store_true", help="include customers that match")
    p.set_defaults(func=cmd_xero_owners)

    p = sub.add_parser("employee-leaves", help="leaver routine: move their accounts to House, deactivate")
    p.add_argument("email")
    p.add_argument("last_day", help="YYYY-MM-DD, last working day")
    p.set_defaults(func=cmd_employee_leaves)


def _add_arr_commands(sub: Any) -> None:
    """ARR position, ARR refs."""
    p = sub.add_parser("arr", help="ARR position by contract")
    p.add_argument("--as-of", help="YYYY-MM-DD (default today)")
    p.set_defaults(func=cmd_arr)

    p = sub.add_parser("link-arr", help="attach the ARR ref to a recurring line (allowed after processing)")
    p.add_argument("order_number", help="SN or SO- number")
    p.add_argument("line_number", type=int)
    p.add_argument("arr_ref", help="e.g. NAP008-26, from the ARR file")
    p.set_defaults(func=cmd_link_arr)

    p = sub.add_parser("arr-outstanding", help="report processed recurring lines missing an ARR ref")
    p.set_defaults(func=cmd_arr_outstanding)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sales-orders", description=__doc__)
    parser.add_argument("--actor", help="who is acting (audit trail); default $SALES_ORDERS_ACTOR or OS user")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="upgrade the database to the latest schema").set_defaults(func=cmd_migrate)
    _add_order_commands(sub)
    _add_master_data_commands(sub)
    _add_arr_commands(sub)
    _add_credit_commands(sub)
    _add_credit_job_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"input rejected:\n{exc}", file=sys.stderr)
    except SalesOrderError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
    except XeroFormatError as exc:
        print(f"Xero: {exc}", file=sys.stderr)
    except (ArrFileError, GraphError, StateStoreError) as exc:
        print(f"rejected: {exc}", file=sys.stderr)
    except OSError as exc:  # Xero unreachable or refused the request: nothing was loaded
        print(f"Xero request failed: {exc}", file=sys.stderr)
    except psycopg.Error as exc:  # constraint / trigger violations: nothing was committed
        detail = exc.diag.message_primary or str(exc)
        print(f"rejected by database: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
