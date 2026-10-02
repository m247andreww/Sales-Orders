"""The credit assessment summary page as a PDF (the snapshot filed on the Xero contact).

Same content and order as the workbook's summary page (Experian, Creditsafe, baseline, workings by
invoicing frequency, one-off allowance, VAT, Total Acceptable Credit Risk), plus what the workbook
never recorded: the ARR file version used, the outcome and who decided. Output is byte-for-byte
reproducible for the same input (reportlab invariant mode), so its SHA-256 identifies it.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

_FREQUENCY_LABELS = {
    "quint-annual": "ARR - quint-annual invoicing",
    "tri-annual": "ARR - tri-annual invoicing",
    "annual": "ARR - annual invoicing",
    "quarterly": "ARR - quarterly invoicing",
    "monthly": "ARR - monthly invoicing",
}
_ALWAYS_SHOWN = ("quint-annual", "tri-annual", "annual", "quarterly", "monthly")


@dataclass(frozen=True)
class SnapshotLine:
    arr_frequency: str
    annual_revenue: Decimal
    exposure: Decimal


@dataclass(frozen=True)
class SnapshotData:
    company_name: str
    assessment_id: int
    assessed_at: datetime
    experian_limit: Decimal | None
    experian_date: datetime | None
    experian_band: str | None
    creditsafe_limit: Decimal | None
    creditsafe_date: datetime | None
    baseline: Decimal | None
    lines: tuple[SnapshotLine, ...]
    one_off_allowance: Decimal
    net_requirement: Decimal
    vat_rate: Decimal
    vat: Decimal
    gross_requirement: Decimal
    trading_requirement: Decimal
    risk_appetite: Decimal
    appetite_pct: Decimal
    appetite_applies: bool
    credit_limit: Decimal | None  # the limit set (None: not a customer)
    decision: str  # e.g. "Applied automatically (within risk appetite)" / "CFO decision: ..."
    arr_source: str | None


def _gbp(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"£{value:,.0f}" if value == value.to_integral_value() else f"£{value:,.2f}"


def _date(value: datetime | None) -> str:
    return value.strftime("%d-%b-%y") if value else ""


def _wrap(text: str, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        if len(line) + len(w) + 1 > width and line:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    return lines


def file_name(data: SnapshotData) -> str:
    safe = "".join(ch for ch in data.company_name if ch.isalnum() or ch in " &-").strip().replace(" ", "_")
    limit = f"{data.credit_limit:.0f}" if data.credit_limit is not None else "NA"
    return f"{safe}_CLA_{data.assessed_at:%Y-%m-%d}_{data.assessment_id}__{limit}.pdf"


class _Page:
    """A single A4 page written top to bottom in the workbook's three columns."""

    LEFT, AMOUNT, VALUE, DATE = 20 * mm, 130 * mm, 165 * mm, 172 * mm

    def __init__(self, c: canvas.Canvas) -> None:
        self.c = c
        self.y = A4[1] - 25 * mm

    def row(self, label: str, amount: str = "", value: str = "", date: str = "", bold: bool = False) -> None:
        self.c.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
        self.c.drawString(self.LEFT, self.y, label)
        if amount:
            self.c.drawRightString(self.AMOUNT, self.y, amount)
        if value:
            self.c.drawRightString(self.VALUE, self.y, value)
        if date:
            self.c.drawString(self.DATE, self.y, date)
        self.y -= 6 * mm

    def gap(self, size: float = 3) -> None:
        self.y -= size * mm

    def text(self, body: str, size: int = 9) -> None:
        self.c.setFont("Helvetica", size)
        for line in _wrap(body, 95):
            self.c.drawString(self.LEFT, self.y, line)
            self.y -= 5 * mm


def _workings(page: _Page, data: SnapshotData) -> None:
    page.row("Credit Limit Workings:", bold=True)
    by_freq = {ln.arr_frequency: ln for ln in data.lines}
    for freq in list(_ALWAYS_SHOWN) + sorted(f for f in by_freq if f not in _ALWAYS_SHOWN):
        ln = by_freq.get(freq)
        page.row(
            _FREQUENCY_LABELS.get(freq, f"ARR - {freq} invoicing"),
            _gbp(ln.annual_revenue if ln else Decimal(0)),
            _gbp(ln.exposure if ln else Decimal(0)),
        )
    page.row("One off & project spend", _gbp(data.one_off_allowance), _gbp(data.one_off_allowance))
    page.row("", value=_gbp(data.net_requirement))
    page.row(f"VAT ({data.vat_rate * 100:.0f}%)", value=_gbp(data.vat))
    page.row("", value=_gbp(data.gross_requirement))
    page.row("Trading requirement (rounded up)", value=_gbp(data.trading_requirement), bold=True)


def render(data: SnapshotData) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(f"Credit Limit Assessment - {data.company_name}")
    c.setAuthor("Managed247 Sales Orders database")
    page = _Page(c)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(page.LEFT, page.y, "Credit Limit Assessment")
    c.drawRightString(190 * mm, page.y, data.company_name[:40])
    page.gap(12)

    page.row("Experian suggested Credit Limit", _gbp(data.experian_limit), date=_date(data.experian_date))
    page.row("Creditsafe", _gbp(data.creditsafe_limit), date=_date(data.creditsafe_date))
    page.row("Baseline (lower of the two)", _gbp(data.baseline), bold=True)
    if data.experian_band:
        page.row(f"Experian risk band: {data.experian_band}")
    page.gap()
    _workings(page, data)
    page.gap()
    page.row(f"Risk appetite: {data.appetite_pct * 100:.0f}% of baseline", value=_gbp(data.risk_appetite))
    page.row(
        "Appetite applies (at most the policy threshold)?", value="TRUE" if data.appetite_applies else "FALSE"
    )
    page.gap()
    page.row("Total Acceptable Credit Risk", value=_gbp(data.credit_limit), bold=True)
    page.gap(2)
    page.text(data.decision)

    page.y = 15 * mm
    page.text(
        f"Assessment {data.assessment_id}, {data.assessed_at:%d %b %Y %H:%M} UTC. "
        f"ARR source: {data.arr_source or 'not used (not a customer)'}.",
        size=7,
    )
    page.text(
        "Exposure: annual and multi-year invoicing at one year's value; quarterly one quarter; monthly two "
        "months. Generated by the Sales Orders database; no figure is typed in.",
        size=7,
    )
    c.showPage()
    c.save()
    return buf.getvalue()
