"""Parser for the AW SOs Register (CSV export of the "Register" tab / "Sales Orders Extract for Claude").

The Register is the master of SN references. Its layout has traps, handled explicitly:
  * two columns are headed "Client" (customer, then the customer's signatory);
  * two are headed "Revenue" (the value, then a TRUE/FALSE check);
  * the SN column's header is a formula result (e.g. "250532"), so it is located as the
    column immediately after "Reporting Category";
  * money is text such as " £ 5,400 " or " £ (1,234) ";
  * dates are "5 Jan 24".
Rows that cannot be trusted are rejected with a reason, never guessed.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

SN_PATTERN = re.compile(r"^([0-9]{4}|[0-9]{6})(LO|CA)?$")
_MONEY = re.compile(r"^\(?-?[0-9,]*\.?[0-9]*\)?$")


class RegisterFormatError(ValueError):
    """The export does not have the expected Register layout: nothing is loaded."""


@dataclass(frozen=True)
class RegisterRow:
    sn_ref: str
    date_issued: date | None
    document_type_raw: str | None
    client: str
    new_logo: str | None
    project: str | None
    customer_po: str | None
    ticket_ref: str | None
    document_date: date | None
    signed_by_managed247: str | None
    signed_by_customer: str | None
    order_category_raw: str | None
    reporting_category_raw: str | None
    salesperson: str | None
    revenue: Decimal | None
    expected_costs: Decimal | None


@dataclass(frozen=True)
class Rejection:
    row_number: int
    raw_sn: str | None
    reason: str


@dataclass
class ParsedRegister:
    rows: list[RegisterRow] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def _norm(header: str) -> str:
    return " ".join(header.split()).strip().lower()


def _text(value: str) -> str | None:
    v = value.strip()
    return None if v in {"", "TO CHECK", "XXX", "N/A", "-"} else v


def parse_money(value: str) -> Decimal | None:
    v = value.replace("£", "").replace(" ", "").strip()
    if v in {"", "-"}:
        return None
    negative = v.startswith("(") and v.endswith(")")
    v = v.strip("()").replace(",", "")
    if not _MONEY.match(v):
        raise ValueError(f"not a money value: {value!r}")
    try:
        amount = Decimal(v)
    except InvalidOperation as exc:
        raise ValueError(f"not a money value: {value!r}") from exc
    return -amount if negative else amount


def parse_date(value: str) -> date | None:
    v = value.strip()
    if not v or v.upper() in {"TO CHECK", "TBC", "N/A"}:
        return None
    for fmt in ("%d %b %y", "%d %b %Y", "%d-%b-%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {value!r}")


def canonical_sn(raw: str) -> str | None:
    """'260518' -> 'SN260518'; '250096Lo' -> 'SN250096LO'. None if not a valid SN."""
    v = raw.strip().upper()
    v = v.removeprefix("SN")
    return f"SN{v}" if SN_PATTERN.match(v) else None


def _locate_columns(header: list[str]) -> dict[str, int]:
    names = [_norm(h) for h in header]

    def first(name: str, start: int = 0) -> int:
        try:
            return names.index(name, start)
        except ValueError as exc:
            raise RegisterFormatError(f"Register export has no {name!r} column") from exc

    cols = {
        "date_issued": first("date issued"),
        "document_type_raw": first("type"),
        "client": first("client"),
        "new_logo": first("new logo"),
        "project": first("project"),
        "customer_po": first("cust po#"),
        "ticket_ref": first("ticket ref."),
        "document_date": first("document date"),
        "signed_by_managed247": first("managed"),
        "order_category_raw": first("document type"),
        "reporting_category_raw": first("reporting category"),
        "salesperson": first("salesperson"),
        "revenue": first("revenue"),
        "expected_costs": first("expected costs"),
    }
    cols["signed_by_customer"] = first("client", cols["client"] + 1)  # second "Client" = signatory
    cols["sn"] = cols["reporting_category_raw"] + 1  # header is a formula result, not a name
    if cols["salesperson"] != cols["sn"] + 1:
        raise RegisterFormatError(
            "Register layout changed: expected SN column between Reporting Category and Salesperson"
        )
    return cols


def parse_register(lines: Iterable[str]) -> ParsedRegister:
    reader = csv.reader(lines)
    header: list[str] | None = None
    for candidate in reader:
        if "date issued" in [_norm(h) for h in candidate]:
            header = candidate
            break
    if header is None:
        raise RegisterFormatError("no header row containing 'Date Issued' found")
    cols = _locate_columns(header)

    result = ParsedRegister()
    seen: set[str] = set()
    for row_number, raw in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw):
            continue
        cells = raw + [""] * (len(header) - len(raw))
        raw_sn = cells[cols["sn"]].strip()
        sn = canonical_sn(raw_sn)
        if sn is None:
            result.rejections.append(Rejection(row_number, raw_sn or None, "not a valid SN reference"))
            continue
        if sn in seen:
            result.rejections.append(Rejection(row_number, raw_sn, "duplicate SN reference"))
            continue
        client = _text(cells[cols["client"]])
        if client is None:
            result.rejections.append(Rejection(row_number, raw_sn, "no client"))
            continue
        try:
            row = RegisterRow(
                sn_ref=sn,
                date_issued=parse_date(cells[cols["date_issued"]]),
                document_type_raw=_text(cells[cols["document_type_raw"]]),
                client=client,
                new_logo=_text(cells[cols["new_logo"]]),
                project=_text(cells[cols["project"]]),
                customer_po=_text(cells[cols["customer_po"]]),
                ticket_ref=_text(cells[cols["ticket_ref"]]),
                document_date=parse_date(cells[cols["document_date"]]),
                signed_by_managed247=_text(cells[cols["signed_by_managed247"]]),
                signed_by_customer=_text(cells[cols["signed_by_customer"]]),
                order_category_raw=_text(cells[cols["order_category_raw"]]),
                reporting_category_raw=_text(cells[cols["reporting_category_raw"]]),
                salesperson=_text(cells[cols["salesperson"]]),
                revenue=parse_money(cells[cols["revenue"]]),
                expected_costs=parse_money(cells[cols["expected_costs"]]),
            )
        except ValueError as exc:
            result.rejections.append(Rejection(row_number, raw_sn, str(exc)))
            continue
        seen.add(sn)
        result.rows.append(row)
    return result
