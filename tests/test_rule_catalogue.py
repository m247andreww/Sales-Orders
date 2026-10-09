"""Every exception rule the views can emit is catalogued in sales.exception_rule (and vice versa)."""

from __future__ import annotations

import re

from conftest import ROOT

from sales_orders.db import Connection

# In the view SQL, a rule is emitted as:  'RULE_CODE' AS rule_code   or   'RULE_CODE', 'error'|'warning'|CASE
_EMITTED = re.compile(
    r"'([A-Z][A-Z_]{3,})'(?:\s+AS\s+rule_code|,\s*(?:--[^\n]*\n\s*)*(?:'(?:error|warning)'|CASE))"
)


def _emitted_rule_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted((ROOT / "migrations" / "sql").glob("*.up.sql")):
        codes |= set(_EMITTED.findall(path.read_text(encoding="utf-8")))
    return codes


def test_catalogue_matches_rules_in_views(conn: Connection) -> None:
    # Order rules are catalogued in sales.exception_rule; credit rules (0014) in sales.credit_exception_rule.
    catalogued = {
        r["rule_code"]
        for r in conn.execute(
            "SELECT rule_code FROM sales.exception_rule UNION ALL SELECT rule_code FROM sales.credit_exception_rule"
        ).fetchall()
    }
    emitted = _emitted_rule_codes()
    assert len(emitted) >= 18  # guard against the pattern silently matching nothing
    # NEGATIVE_LINE_MARGIN / LOW_ORDER_MARGIN existed only in 0001's view and were replaced in 0003;
    # NO_DEBT_CREDIT_FOLDER existed only in 0014's credit view and was retired in 0015;
    # NO_CREDIT_TERMS was retired in 0029 (no saved terms = the standard terms, 0026).
    retired = {"NEGATIVE_LINE_MARGIN", "LOW_ORDER_MARGIN", "NO_DEBT_CREDIT_FOLDER", "NO_CREDIT_TERMS"}
    assert emitted - retired == catalogued


def test_credit_rule_severity_matches_the_view(conn: Connection) -> None:
    # the latest migration that (re)defines the view is the one in force
    latest = [
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "migrations" / "sql").glob("*.up.sql"))
        if "VIEW sales.v_credit_exception AS" in p.read_text(encoding="utf-8")
    ][-1]
    view = latest[latest.index("VIEW sales.v_credit_exception AS") :]
    emitted = dict(re.findall(r"SELECT '([A-Z_]+)'(?: AS rule_code)?, '(error|warning)'", view))
    catalogued = {
        r["rule_code"]: r["severity"]
        for r in conn.execute("SELECT rule_code, severity FROM sales.credit_exception_rule").fetchall()
    }
    assert emitted == catalogued


def test_warnings_never_block(conn: Connection) -> None:
    blocking_warnings = conn.execute(
        """SELECT rule_code FROM sales.exception_rule
            WHERE blocks_approval AND description ILIKE '%%(warning)%%'"""
    ).fetchall()
    assert blocking_warnings == []
