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
    catalogued = {
        r["rule_code"] for r in conn.execute("SELECT rule_code FROM sales.exception_rule").fetchall()
    }
    emitted = _emitted_rule_codes()
    assert len(emitted) >= 18  # guard against the pattern silently matching nothing
    # NEGATIVE_LINE_MARGIN / LOW_ORDER_MARGIN existed only in 0001's view and were replaced in 0003.
    assert emitted - {"NEGATIVE_LINE_MARGIN", "LOW_ORDER_MARGIN"} == catalogued


def test_warnings_never_block(conn: Connection) -> None:
    blocking_warnings = conn.execute(
        """SELECT rule_code FROM sales.exception_rule
            WHERE blocks_approval AND description ILIKE '%%(warning)%%'"""
    ).fetchall()
    assert blocking_warnings == []
