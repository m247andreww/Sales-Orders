"""Bureau monitoring alert emails -> one reading per company (migration 0014).

Two formats, both HTML, both sent to the CFO's mailbox:

* Experian Business Express ("New Experian Business Express Alerts", ebe.noreply@experian.com):
  per company a heading "<number> : <NAME>" linking to .../reg-report/<number>/..., then bullet
  points such as "The Credit Rating has changed from £56,000 to £64,000 and Credit Limit has changed
  from £170,000 to £190,000." and "... the Credit Risk Band has moved from High Risk to Below Average
  Risk." The workbook's "Experian suggested Credit Limit" is the Credit Limit figure.
* Creditsafe ("Connect monitoring alert", monitoring@creditsafe.com): a table, one row per event:
  company name / "<number> / <Safe No.>", reference, personal limit, notes, and the event, e.g.
  "Credit Limit / Previous Value: 3200000 / New Value: 5650000".

A body that does not look like either format raises AlertFormatError: the email is recorded as not
read (exception ALERT_NOT_READ) and nothing is guessed from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

EXPERIAN = "experian"
CREDITSAFE = "creditsafe"

_COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")


class AlertFormatError(ValueError):
    """The email is not in a bureau format this module understands."""


@dataclass(frozen=True)
class AlertCompany:
    """What one alert said about one company."""

    company_number: str | None
    bureau_ref: str | None
    company_name: str
    limit_status: str = "not_reported"  # value | not_available | not_reported
    credit_limit: Decimal | None = None
    previous_credit_limit: Decimal | None = None
    credit_rating: Decimal | None = None
    risk_score: int | None = None
    risk_band: str | None = None
    events: tuple[str, ...] = field(default=())


# ------------------------------------------------------------------------- HTML to text blocks


class _Blocks(HTMLParser):
    """Flattens HTML into text blocks: one per paragraph / list item / table cell, with links kept."""

    _BLOCK = frozenset({"p", "li", "td", "th", "h1", "h2", "h3", "tr", "div", "ul", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, str | None]] = []  # (tag, text, first href)
        self._stack: list[tuple[str, list[str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self._stack.append((tag, [], []))
        elif tag == "br" and self._stack:
            self._stack[-1][1].append("\n")
        elif tag == "a" and self._stack:
            href = dict(attrs).get("href")
            if href:
                self._stack[-1][2].append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK and self._stack:
            # close up to the matching tag (tolerates unclosed children)
            while self._stack:
                t, text, hrefs = self._stack.pop()
                content = "".join(text)
                if content.strip() or hrefs or t in ("tr", "td"):  # empty cells/rows keep the grid
                    self.blocks.append((t, content, hrefs[0] if hrefs else None))
                if self._stack and t not in ("tr", "ul", "table"):
                    self._stack[-1][1].append(" ")
                if t == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1][1].append(data)


def _blocks(html: str) -> list[tuple[str, str, str | None]]:
    parser = _Blocks()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", text).strip()


def _money(text: str) -> Decimal | None:
    """'£2,200,000' / '5650000' -> Decimal; 'N/A' or anything non-numeric -> None."""
    cleaned = text.replace("£", "").replace(",", "").strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# ------------------------------------------------------------------------- Experian

_EXP_HEADING = re.compile(r"^\s*([A-Z0-9]{2,12})\s*:\s*(.+?)\s*$")
_EXP_LIMIT = re.compile(r"Credit Limit has changed from (\S+) to (\S+?)\.?(?:\s|$)")
_EXP_RATING = re.compile(r"Credit Rating has changed from (\S+) to (\S+?)\.?(?:\s|$)")
_EXP_SCORE = re.compile(r"Credit Risk Score has changed from (\S+) to (\S+?)\.?(?:\s|$)")
_EXP_BAND = re.compile(r"Credit Risk Band has moved from (.+?) to (.+?)\.?\s*$")


def _experian_company(heading: str, items: list[str]) -> AlertCompany:
    m = _EXP_HEADING.match(heading)
    if m is None:
        raise AlertFormatError(f"Experian company heading not understood: {heading!r}")
    ref, name = m.group(1), _clean(m.group(2))
    company = AlertCompany(
        company_number=ref if _COMPANY_NUMBER.match(ref) else None,
        bureau_ref=None if _COMPANY_NUMBER.match(ref) else ref,
        company_name=name,
        events=tuple(items),
    )
    for item in items:
        if lm := _EXP_LIMIT.search(item):
            new = _money(lm.group(2))
            company = replace(
                company,
                limit_status="value" if new is not None else "not_available",
                credit_limit=new,
                previous_credit_limit=_money(lm.group(1)),
            )
        if rm := _EXP_RATING.search(item):
            company = replace(company, credit_rating=_money(rm.group(2)))
        if sm := _EXP_SCORE.search(item):
            score = sm.group(2)
            company = replace(company, risk_score=int(score) if score.isdigit() else None)
        if bm := _EXP_BAND.search(item):
            band = _clean(bm.group(2))
            company = replace(company, risk_band=None if band.upper() == "N/A" else band)
    return company


def parse_experian(html: str) -> list[AlertCompany]:
    blocks = _blocks(html)
    texts = [_clean(t) for _, t, _ in blocks]
    if not any("Registered Companies" in t for t in texts) or not any("Experian" in t for t in texts):
        raise AlertFormatError("not an Experian Business Express alert (no 'Registered Companies' section)")
    companies: list[AlertCompany] = []
    heading: str | None = None
    items: list[str] = []
    for tag, text, href in blocks:
        clean = _clean(text)
        if tag == "p" and href and "/change-history" in href and _EXP_HEADING.match(clean):
            if heading is not None:
                companies.append(_experian_company(heading, items))
            heading, items = clean, []
        elif tag == "li" and heading is not None:
            items.append(clean)
    if heading is not None:
        companies.append(_experian_company(heading, items))
    return companies


# ------------------------------------------------------------------------- Creditsafe

_CS_IDS = re.compile(r"^\s*([A-Z0-9]{8}|-)?\s*/\s*([A-Z]{2}[A-Z0-9]+)\s*$")
_CS_VALUE = re.compile(r"(Previous|New) Value:\s*(.*)$")


def _creditsafe_rows(blocks: list[tuple[str, str, str | None]]) -> list[list[tuple[str, str | None]]]:
    """Table cells grouped by row (a row ends at its closing tr)."""
    rows: list[list[tuple[str, str | None]]] = []
    current: list[tuple[str, str | None]] = []
    for tag, text, href in blocks:
        if tag == "td":
            current.append((text, href))
        elif tag == "tr":
            if current:
                rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows


def _creditsafe_event(company: AlertCompany, event_cell: str) -> AlertCompany:
    lines = [ln for ln in (_clean(x) for x in event_cell.split("\n")) if ln]
    if not lines:
        return company
    event = lines[0]
    values = {m.group(1): m.group(2).strip() for ln in lines[1:] if (m := _CS_VALUE.search(ln))}
    company = replace(
        company, events=(*company.events, event + "".join(f"; {k} {v}" for k, v in values.items()))
    )
    if event.lower() == "credit limit" and "New" in values:
        new = _money(values["New"])
        return replace(
            company,
            limit_status="value" if new is not None else "not_available",
            credit_limit=new,
            previous_credit_limit=_money(values.get("Previous", "")),
        )
    if "score" in event.lower() and values.get("New", "").isdigit():
        return replace(company, risk_score=int(values["New"]))
    return company


def parse_creditsafe(html: str) -> list[AlertCompany]:
    blocks = _blocks(html)
    texts = [_clean(t) for _, t, _ in blocks]
    if not any("Monitoring Alert" in t for t in texts) or not any("Company No." in t for t in texts):
        raise AlertFormatError("not a Creditsafe monitoring alert (no 'Company No.' table)")
    by_key: dict[str, AlertCompany] = {}
    for cells in _creditsafe_rows(blocks):
        # A data row: 5 cells (name + ids, reference, personal limit, notes, event), the first
        # linking to the company on app.creditsafe.com. Layout rows around the table are skipped.
        if len(cells) != 5 or not (cells[0][1] or "").startswith("https://app.creditsafe.com/companies/"):  # noqa: PLR2004
            continue
        first = [ln for ln in (_clean(x) for x in cells[0][0].split("\n")) if ln]
        ids = _CS_IDS.match(first[-1]) if len(first) >= 2 else None  # noqa: PLR2004 - name line + ids line
        if ids is None:
            raise AlertFormatError(f"Creditsafe company cell not understood: {cells[0][0]!r}")
        number = ids.group(1) if ids.group(1) and ids.group(1) != "-" else None
        key = ids.group(2)
        company = by_key.get(
            key, AlertCompany(company_number=number, bureau_ref=key, company_name=" ".join(first[:-1]))
        )
        by_key[key] = _creditsafe_event(company, cells[4][0])
    return list(by_key.values())


def parse_alert(bureau: str, html: str) -> list[AlertCompany]:
    if bureau == EXPERIAN:
        return parse_experian(html)
    if bureau == CREDITSAFE:
        return parse_creditsafe(html)
    raise AlertFormatError(f"unknown bureau {bureau!r}")
