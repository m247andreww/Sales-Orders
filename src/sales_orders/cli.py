"""Command-line interface: `sales-orders --help`."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
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
    print(f"Customer: {s['customer_name']}   Status: {s['status_code']}   Type: {s['order_type_code']}")
    print(
        f"{'#':>2} {'Description':<44} {'Qty':>7} {'Per':>3} {'Cost':>12} {'Sell':>12} {'GM':>11} {'GM%':>6}"
    )
    for ln in lines:
        print(
            f"{ln['line_number']:>2} {ln['description'][:44]:<44} {ln['quantity']:>7.2f} "
            f"{ln['billing_periods']:>3} {m(ln['net_cost'], ccy):>12} {m(ln['net_sell'], ccy):>12} "
            f"{m(ln['gross_margin'], ccy):>11} {_pct(ln['gross_margin_pct']):>6}"
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
            checked_by_email=args.by,
            notes=args.notes,
            evidence_sha256=args.evidence,
        )
    print(f"{args.order_number}: {args.check_type} = {args.status}")
    return 0


def _pct(value: Any) -> str:
    return "-" if value is None else f"{value:.1f}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sales-orders", description=__doc__)
    parser.add_argument("--actor", help="who is acting (audit trail); default $SALES_ORDERS_ACTOR or OS user")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="upgrade the database to the latest schema").set_defaults(func=cmd_migrate)

    p = sub.add_parser("load-master-data", help="load employees, suppliers, customers, FX rates")
    p.add_argument("file")
    p.set_defaults(func=cmd_load_master_data)

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
    p.add_argument("--by", required=True, help="email of the employee who performed the check")
    p.add_argument("--notes")
    p.add_argument("--evidence", help="sha256 of a supporting document already recorded")
    p.set_defaults(func=cmd_check)
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
