"""Command-line interface: `sales-orders --help`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import psycopg
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ValidationError

from sales_orders import service
from sales_orders.db import unit_of_work
from sales_orders.errors import SalesOrderError
from sales_orders.models import MasterDataIn, OrderSubmissionIn
from sales_orders.register import parse_register

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"input rejected:\n{exc}", file=sys.stderr)
    except SalesOrderError as exc:
        print(f"rejected: {exc}", file=sys.stderr)
    except psycopg.Error as exc:  # constraint / trigger violations: nothing was committed
        detail = exc.diag.message_primary or str(exc)
        print(f"rejected by database: {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
