"""ARR file (ARR File Master.xlsx, sheet "Master Data") -> recurring commitments (migration 0014).

The credit assessment needs, per ARR line: customer, internal ref (its 3-letter prefix identifies the
customer, e.g. AGE001), order status, invoicing frequency and annual revenue ("Ann Rev"). Columns
are found by their header text, never by position, because the sheet's layout changes.

Excel stores numbers as binary floats; each amount is converted to Decimal via its shortest decimal
representation and rounded to pence at this boundary, so nothing downstream ever sees a float.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from openpyxl import load_workbook

SHEET = "Master Data"
# header text (as it appears in the file) -> field
_HEADERS = {
    "Order Status": "status",
    "Customer": "customer",
    "Internal Ref.": "internal_ref",
    "Frequency": "frequency",
    "Ann Rev": "annual_revenue",
}
_PENNY = Decimal("0.01")


EXTRACT_SHEET = "Credit Extract"
_EXTRACT_HEADING = re.compile("^## Sheet: (.+?) — ([0-9]+) rows \u00d7 ([0-9]+) columns")


class ArrFileError(ValueError):
    """The ARR file is not in the expected layout: nothing is loaded."""


@dataclass(frozen=True)
class ArrLine:
    source_row: int
    customer_name: str
    internal_ref: str | None
    status: str
    frequency: str  # lower case, as sales.credit_exposure_rule
    annual_revenue: Decimal


@dataclass(frozen=True)
class ParsedArrFile:
    source_name: str
    sha256: str
    lines: tuple[ArrLine, ...]
    skipped: tuple[tuple[int, str], ...]  # (row, reason): rows with no amount, reported not loaded


def _amount(value: Any, row: int) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise ArrFileError(f"row {row}: Ann Rev is not a number: {value!r}")
    try:
        return Decimal(str(value)).quantize(_PENNY, rounding=ROUND_HALF_UP)
    except ArithmeticError as exc:
        raise ArrFileError(f"row {row}: Ann Rev is not a number: {value!r}") from exc


def _lines(
    rows: list[tuple[Any, ...]], columns: dict[str, int], first_row: int
) -> tuple[list[ArrLine], list[tuple[int, str]]]:
    """ARR lines from data rows (after the header); rows with no amount are reported, not loaded."""
    lines: list[ArrLine] = []
    skipped: list[tuple[int, str]] = []
    for offset, row in enumerate(rows, start=first_row):

        def cell(field: str, row: tuple[Any, ...] = row) -> Any:
            j = columns[field]
            return row[j] if j < len(row) else None

        customer = cell("customer")
        if customer is None or not str(customer).strip():
            continue
        status, frequency = cell("status"), cell("frequency")
        if not status or not frequency:
            skipped.append((offset, "no order status or frequency"))
            continue
        amount = _amount(cell("annual_revenue"), offset)
        if amount is None:
            skipped.append((offset, f"{str(customer).strip()}: no Ann Rev"))
            continue
        ref = cell("internal_ref")
        lines.append(
            ArrLine(
                source_row=offset,
                customer_name=str(customer).strip(),
                internal_ref=str(ref).strip() if ref else None,
                status=str(status).strip(),
                frequency=str(frequency).strip().lower(),
                annual_revenue=amount,
            )
        )
    return lines, skipped


def parse_arr_file(content: bytes, source_name: str) -> ParsedArrFile:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for a damaged/non-xlsx file
        raise ArrFileError(f"{source_name} is not a readable .xlsx workbook: {exc}") from exc
    if SHEET not in wb.sheetnames:
        raise ArrFileError(f"{source_name} has no '{SHEET}' sheet")
    rows = list(wb[SHEET].iter_rows(values_only=True))

    header_index: int | None = None
    columns: dict[str, int] = {}
    for i, row in enumerate(rows[:10]):
        labels = {str(v).strip(): j for j, v in enumerate(row) if v is not None}
        if all(h in labels for h in _HEADERS):
            header_index, columns = i, {f: labels[h] for h, f in _HEADERS.items()}
            break
    if header_index is None:
        raise ArrFileError(f"'{SHEET}' has no header row with: {', '.join(_HEADERS)}")

    lines, skipped = _lines(rows[header_index + 1 :], columns, header_index + 2)
    return ParsedArrFile(
        source_name=source_name,
        sha256=hashlib.sha256(content).hexdigest(),
        lines=tuple(lines),
        skipped=tuple(skipped),
    )


def parse_arr_extract(text: str, source_name: str) -> ParsedArrFile:
    """The ARR file's "Credit Extract" sheet, as the Microsoft 365 connector reads a workbook.

    The sheet is one formula, =FILTER(CHOOSECOLS('Master Data'!A3:R5000,1,2,3,15,18), ...), so it holds
    raw (unformatted) values for the five columns the assessment needs. The connector renders each
    sheet as "## Sheet: <name> — <rows> rows x <cols> columns" followed by tab-separated rows. The read
    is refused unless every row the heading announces is present (no truncation, nothing guessed).
    """
    lines_in = text.splitlines()
    start = next(
        (
            i
            for i, ln in enumerate(lines_in)
            if (m := _EXTRACT_HEADING.match(ln)) and m.group(1) == EXTRACT_SHEET
        ),
        None,
    )
    if start is None:
        raise ArrFileError(f"{source_name}: no '{EXTRACT_SHEET}' sheet in the connector output")
    heading = _EXTRACT_HEADING.match(lines_in[start])
    if heading is None:  # pragma: no cover - matched above
        raise ArrFileError("internal error: heading vanished")
    declared = int(heading.group(2))
    body: list[str] = []
    for ln in lines_in[start + 1 :]:
        if ln.startswith(("## Sheet:", "Formulas")) or not ln.strip():
            break
        if ln.startswith("[Output truncated"):
            raise ArrFileError(f"{source_name}: the '{EXTRACT_SHEET}' sheet was truncated by the connector")
        body.append(ln)
    if len(body) != declared:
        raise ArrFileError(
            f"{source_name}: '{EXTRACT_SHEET}' announces {declared} rows but {len(body)} were read: incomplete"
        )
    rows = [tuple(cell if cell != "" else None for cell in ln.split("\t")) for ln in body]
    labels = {str(v).strip(): j for j, v in enumerate(rows[0]) if v is not None}
    missing = [h for h in _HEADERS if h not in labels]
    if missing:
        raise ArrFileError(f"{source_name}: '{EXTRACT_SHEET}' lacks columns {missing}")
    lines, skipped = _lines(rows[1:], {f: labels[h] for h, f in _HEADERS.items()}, 2)
    section = "\n".join([lines_in[start], *body])
    return ParsedArrFile(
        source_name=f"{source_name} ({EXTRACT_SHEET})",
        sha256=hashlib.sha256(section.encode()).hexdigest(),
        lines=tuple(lines),
        skipped=tuple(skipped),
    )
