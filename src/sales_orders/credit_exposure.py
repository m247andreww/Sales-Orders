"""Credit exposure: what each customer owes plus what is in progress, against its credit limit (0021).

Inputs are two daily files the credit job writes from the CFO's Claude connectors (the Xero custom
connection has no invoice access): Xero aged receivables and in-progress PandaDoc documents. Both are
loaded as insert-only snapshots, all or nothing; amounts are Decimal strings, never floats.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

from sales_orders.db import Connection
from sales_orders.errors import SalesOrderError

_NOISE = re.compile(r"\b(limited|ltd|plc|llp|uk|group|holdings?|the|services)\b|[^a-z0-9]")
_NAME_SPLIT = re.compile(r"\s+[-\u2013\u2014|]\s+|\s*\(")  # hyphen, en dash, em dash, bar


def _norm(name: str) -> str:
    return _NOISE.sub("", name.lower().replace("&", "and"))


def _money(value: Any, what: str) -> Decimal:
    if isinstance(value, float):
        raise SalesOrderError(f"{what}: amounts must be strings, not floats ({value!r})")
    try:
        return Decimal(str(value))
    except ArithmeticError as exc:
        raise SalesOrderError(f"{what}: not an amount: {value!r}") from exc


def _sha(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _directory(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT xero_contact_id, name FROM sales.xero_contact_directory WHERE contact_status = 'ACTIVE'"
    ).fetchall()


def load_receivables(conn: Connection, document: dict[str, Any]) -> tuple[int, list[str]]:
    """Load one aged-receivables file. Returns (snapshot id, names not matched to a Xero contact)."""
    by_id = {str(r["xero_contact_id"]) for r in _directory(conn)}
    by_name: dict[str, list[str]] = {}
    for r in _directory(conn):
        by_name.setdefault(str(r["name"]).strip().lower(), []).append(str(r["xero_contact_id"]))
    row = conn.execute(
        """INSERT INTO sales.credit_receivable_snapshot (as_of, source, source_sha256) VALUES (%s, %s, %s)
           ON CONFLICT (source_sha256) DO NOTHING RETURNING credit_receivable_snapshot_id""",
        (date.fromisoformat(document["as_of"]), document["source"], _sha(document)),
    ).fetchone()
    if row is None:
        raise SalesOrderError("this receivables file is already loaded")
    snap = int(row["credit_receivable_snapshot_id"])
    unmatched = []
    for i, c in enumerate(document["contacts"], start=1):
        cid = c.get("xero_contact_id")
        if cid not in by_id:
            ids = by_name.get(str(c["name"]).strip().lower(), [])
            cid = ids[0] if len(ids) == 1 else None
        if cid is None:
            unmatched.append(str(c["name"]))
        conn.execute(
            """INSERT INTO sales.credit_receivable_line
                   (credit_receivable_snapshot_id, line_no, contact_name, xero_contact_id, current_amount,
                    overdue_amount, overdue_over_60, oldest_due_date, invoice_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                snap,
                i,
                c["name"],
                cid,
                _money(c["current"], c["name"]),
                _money(c["overdue"], c["name"]),
                _money(c.get("overdue_over_60") or "0", c["name"]),
                date.fromisoformat(c["oldest_due_date"]) if c.get("oldest_due_date") else None,
                int(c.get("invoice_count") or 0),
            ),
        )
    return snap, unmatched


def _pipeline_match(conn: Connection, doc: dict[str, Any]) -> tuple[str | None, str | None]:
    """The single Xero contact a PandaDoc document is for, by its client company, then its name."""
    names: dict[str, set[str]] = {}
    for r in _directory(conn):
        names.setdefault(_norm(str(r["name"])), set()).add(str(r["xero_contact_id"]))
    for r in conn.execute(
        """SELECT c.legal_name, c.xero_contact_id FROM sales.customer c WHERE c.xero_contact_id IS NOT NULL
           UNION SELECT l.customer_name, m.xero_contact_id
             FROM sales.credit_customer_match m
             JOIN sales.credit_arr_line l ON l.arr_prefix = m.arr_prefix
            WHERE m.xero_contact_id IS NOT NULL"""
    ):
        names.setdefault(_norm(str(r["legal_name"])), set()).add(str(r["xero_contact_id"]))

    def one(text: str | None) -> str | None:
        key = _norm(text or "")
        if len(key) < MIN_KEY:
            return None
        hits = names.get(key) or set()
        if not hits:
            starts = {
                cid
                for k, ids in names.items()
                if k.startswith(key) or (len(k) >= MIN_KEY and key.startswith(k))
                for cid in ids
            }
            hits = starts
        return next(iter(hits)) if len(hits) == 1 else None

    if cid := one(doc.get("client_company")):
        return cid, "client_company"
    if cid := one(_NAME_SPLIT.split(str(doc["name"]), maxsplit=1)[0]):
        return cid, "document_name"
    return None, None


MIN_KEY = 3


def load_pipeline(conn: Connection, document: dict[str, Any]) -> tuple[int, list[str]]:
    """Load one in-progress PandaDoc file. Returns (snapshot id, documents not matched to a customer)."""
    row = conn.execute(
        """INSERT INTO sales.credit_pipeline_snapshot (as_of, source, source_sha256) VALUES (%s, %s, %s)
           ON CONFLICT (source_sha256) DO NOTHING RETURNING credit_pipeline_snapshot_id""",
        (date.fromisoformat(document["as_of"]), document["source"], _sha(document)),
    ).fetchone()
    if row is None:
        raise SalesOrderError("this PandaDoc file is already loaded")
    snap = int(row["credit_pipeline_snapshot_id"])
    unmatched = []
    for d in document["documents"]:
        cid, method = _pipeline_match(conn, d)
        if cid is None:
            unmatched.append(str(d["name"]))
        conn.execute(
            """INSERT INTO sales.credit_pipeline_document
                   (credit_pipeline_snapshot_id, document_id, name, status, date_sent, expiration_date, grand_total,
                    currency, client_company, recipient_domains, xero_contact_id, match_method)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                snap,
                d["id"],
                d["name"],
                d["status"],
                d.get("date_sent"),
                d.get("expiration_date"),
                _money(d["grand_total"], d["name"]),
                d.get("currency") or "GBP",
                d.get("client_company"),
                list(d.get("recipient_domains") or []),
                cid,
                method,
            ),
        )
    return snap, unmatched


def _status(r: dict[str, Any]) -> str:
    if r["credit_limit"] is None:
        return "no_limit" if r["exposure_if_signed"] > 0 else "no_limit_nothing_owed"
    if r["exposure"] > r["credit_limit"]:
        return "over_limit"
    return "over_if_signed" if r["exposure_if_signed"] > r["credit_limit"] else "within"


_ORDER = {"over_limit": 0, "no_limit": 1, "over_if_signed": 2, "within": 3, "no_limit_nothing_owed": 4}


def exposure_rows(conn: Connection) -> list[dict[str, Any]]:
    """Every customer: owed (current / overdue), in progress, limit, headroom; worst first."""
    rows = conn.execute(
        """SELECT xero_contact_id, name, monitored, excluded_reason, arr_prefixes, annual_recurring, credit_limit,
                  current_amount, overdue_amount, overdue_over_60, outstanding, oldest_due_date, pipeline_count,
                  pipeline_value, pipeline_largest, pipeline_gross, exposure, headroom, exposure_if_signed,
                  headroom_if_signed
             FROM sales.v_credit_exposure"""
    ).fetchall()
    out = []
    for r in rows:
        status = _status(r)
        out.append({**r, "status": status, "xero_contact_id": str(r["xero_contact_id"])})
    return sorted(out, key=lambda r: (_ORDER[r["status"]], -r["exposure_if_signed"], r["name"] or ""))


def latest_unmatched(conn: Connection) -> dict[str, list[dict[str, Any]]]:
    """Receivable lines and PandaDoc documents in the latest snapshots that matched no customer."""
    return {
        "receivables": conn.execute(
            """SELECT contact_name AS name, outstanding FROM sales.credit_receivable_line
                WHERE xero_contact_id IS NULL AND credit_receivable_snapshot_id =
                      (SELECT max(credit_receivable_snapshot_id) FROM sales.credit_receivable_snapshot)
                ORDER BY outstanding DESC"""
        ).fetchall(),
        "pipeline": conn.execute(
            """SELECT name, status, grand_total, client_company FROM sales.credit_pipeline_document
                WHERE xero_contact_id IS NULL AND credit_pipeline_snapshot_id =
                      (SELECT max(credit_pipeline_snapshot_id) FROM sales.credit_pipeline_snapshot)
                ORDER BY grand_total DESC"""
        ).fetchall(),
    }
