"""Loading master data from its sources: Xero (customers, chart of accounts) and the Reference sheet.

Each source stays the master of its data; these functions only bring the database in line and
report what they changed. They never delete: a contact archived in Xero is marked, not removed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from sales_orders import service
from sales_orders.db import Connection
from sales_orders.models import GlAccountIn, MasterDataIn
from sales_orders.reference import AccountMove, StaffMember, staff_master_data
from sales_orders.xero import XeroAccount, XeroCustomer

_GL_CODE = re.compile(r"^[0-9]{4}[A-Z]{0,2}$")
_GL_CLASSES = {"ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"}


@dataclass
class SyncCounts:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self, noun: str) -> str:
        text = f"{noun}: {self.added} added, {self.updated} updated, {self.unchanged} unchanged"
        if self.skipped:
            text += f"; {len(self.skipped)} need attention: " + "; ".join(self.skipped[:20])
        return text


def sync_xero_customers(conn: Connection, customers: Sequence[XeroCustomer]) -> SyncCounts:
    """Xero is the master of customer names; the Xero contact id is the key that never changes."""
    counts = SyncCounts()
    for c in customers:
        row = service._one(
            conn,
            "SELECT customer_id, legal_name, xero_archived FROM sales.customer WHERE xero_contact_id = %s",
            (c.contact_id,),
        )
        if row is None:
            same_name = service._one(
                conn,
                "SELECT customer_id, xero_contact_id FROM sales.customer WHERE lower(legal_name) = lower(%s)",
                (c.name,),
            )
            if same_name is not None and same_name["xero_contact_id"] is not None:
                counts.skipped.append(f"{c.name!r} is already linked to another Xero contact")
                continue
            if same_name is not None:
                conn.execute(
                    "UPDATE sales.customer SET xero_contact_id = %s, xero_archived = %s WHERE customer_id = %s",
                    (c.contact_id, c.archived, same_name["customer_id"]),
                )
                counts.updated += 1
            else:
                conn.execute(
                    "INSERT INTO sales.customer (legal_name, xero_contact_id, xero_archived) VALUES (%s, %s, %s)",
                    (c.name, c.contact_id, c.archived),
                )
                counts.added += 1
            continue
        if (row["legal_name"], row["xero_archived"]) == (c.name, c.archived):
            counts.unchanged += 1
            continue
        try:
            with conn.transaction():
                conn.execute(
                    "UPDATE sales.customer SET legal_name = %s, xero_archived = %s WHERE customer_id = %s",
                    (c.name, c.archived, row["customer_id"]),
                )
            counts.updated += 1
        except psycopg.errors.UniqueViolation:
            counts.skipped.append(f"cannot rename {row['legal_name']!r} to {c.name!r}: that name is taken")
    return counts


def sync_xero_accounts(conn: Connection, accounts: Sequence[XeroAccount]) -> SyncCounts:
    """Mirror the Xero chart of accounts (codes in the 4-digit Managed247 format)."""
    counts = SyncCounts()
    load: list[GlAccountIn] = []
    for a in accounts:
        if a.code is None or not _GL_CODE.match(a.code) or a.account_class not in _GL_CLASSES:
            counts.skipped.append(f"{a.code or '(no code)'} {a.name}")
            continue
        before = service._one(
            conn,
            "SELECT name, account_class, account_type, tax_type, is_active FROM sales.gl_account WHERE account_code = %s",
            (a.code,),
        )
        after = {
            "name": a.name,
            "account_class": a.account_class,
            "account_type": a.account_type,
            "tax_type": a.tax_type,
            "is_active": a.active,
        }
        if before is None:
            counts.added += 1
        elif dict(before) == after:
            counts.unchanged += 1
        else:
            counts.updated += 1
        load.append(
            GlAccountIn(
                account_code=a.code,
                name=a.name,
                account_class=a.account_class,
                account_type=a.account_type,
                tax_type=a.tax_type,
                xero_account_id=a.account_id,
            )
        )
    service.load_master_data(conn, MasterDataIn(gl_accounts=tuple(load)))
    loaded = {g.account_code for g in load}
    for a in accounts:
        if a.code in loaded:  # archived in Xero: kept (old lines use it) but no longer offered
            conn.execute(
                "UPDATE sales.gl_account SET is_active = %s WHERE account_code = %s AND is_active IS DISTINCT FROM %s",
                (a.active, a.code, a.active),
            )
    return counts


def load_staff(conn: Connection, staff: Sequence[StaffMember]) -> str:
    """Employees, Register labels and Xero owner groups; then each recorded last working day."""
    service.load_master_data(conn, staff_master_data(staff))
    moved: list[str] = []
    for s in staff:
        if s.last_working_day is not None:
            n = service.employee_leaves(conn, s.email, s.last_working_day)
            if n:
                moved.append(f"{s.name}: {n} account(s) to House")
    leavers = sum(1 for s in staff if s.last_working_day is not None)
    text = f"staff: {len(staff)} people, {leavers} leaver(s)"
    return text + (f"; {'; '.join(moved)}" if moved else "")


def apply_account_moves(conn: Connection, moves: Sequence[AccountMove]) -> str:
    changed: list[str] = []
    for m in moves:
        rows: list[dict[str, Any]] = conn.execute(
            "SELECT customer, result FROM sales.move_accounts(%s, %s, %s, %s)",
            (m.from_email, m.to, m.effective, m.reason),
        ).fetchall()
        done = [r for r in rows if r["result"] != "already moved"]
        changed.extend(f"row {m.row} {r['customer']}: {r['result']}" for r in done)
    return f"account moves: {len(moves)} row(s), {len(changed)} change(s)" + (
        f"; {'; '.join(changed[:20])}" if changed else ""
    )
