"""The CFO-maintained "Sales Orders - Reference" sheet: staff and account moves.

Microsoft 365 is not used as the staff source (CFO, 2026-09-25), so the CFO keeps this sheet and the
nightly job loads it. Tabs and headings are fixed: if they change, the load stops with a clear
reason rather than guessing. Dates are UK style, dd/mm/yyyy.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sales_orders.models import EmployeeIn, MasterDataIn, RegisterOwnerAliasIn, XeroOwnerGroupIn

STAFF_TAB = "Staff"
MOVES_TAB = "Account moves"
STAFF_HEADERS = (
    "Name",
    "Work email",
    "Job title",
    "Register labels",
    "Xero owner group",
    "Last working day",
    "Notes",
)
MOVES_HEADERS = ("From", "To", "Effective date", "Reason")
HOUSE = "House"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ReferenceFormatError(ValueError):
    """The Reference sheet does not have the agreed layout or contains an invalid value."""


@dataclass(frozen=True)
class StaffMember:
    name: str
    email: str
    job_title: str | None
    register_labels: tuple[str, ...]
    xero_group: str | None
    last_working_day: date | None


@dataclass(frozen=True)
class AccountMove:
    row: int
    from_email: str
    to: str  # an email, or "House"
    effective: date
    reason: str


def _cell(row: Sequence[str], i: int) -> str:
    return row[i].strip() if i < len(row) else ""


def _date(value: str, where: str) -> date:
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except ValueError as exc:
        raise ReferenceFormatError(f"{where}: {value!r} is not a date in dd/mm/yyyy form") from exc


def _email(value: str, where: str) -> str:
    if not _EMAIL.match(value):
        raise ReferenceFormatError(f"{where}: {value!r} is not an email address")
    return value.lower()


def _body(
    rows: Sequence[Sequence[str]], tab: str, headers: tuple[str, ...]
) -> list[tuple[int, Sequence[str]]]:
    if not rows or tuple(_cell(rows[0], i) for i in range(len(headers))) != headers:
        found = [c.strip() for c in rows[0]] if rows else []
        raise ReferenceFormatError(f"{tab} tab: headings must be {list(headers)}, found {found}")
    return [(n, r) for n, r in enumerate(rows[1:], start=2) if any(c.strip() for c in r)]


def parse_staff(rows: Sequence[Sequence[str]]) -> list[StaffMember]:
    staff: list[StaffMember] = []
    emails: set[str] = set()
    labels: dict[str, str] = {}
    for n, r in _body(rows, STAFF_TAB, STAFF_HEADERS):
        where = f"{STAFF_TAB} row {n}"
        name = _cell(r, 0)
        if not name:
            raise ReferenceFormatError(f"{where}: Name is empty")
        email = _email(_cell(r, 1), where)
        if email in emails:
            raise ReferenceFormatError(f"{where}: {email} appears twice")
        emails.add(email)
        row_labels = tuple(x.strip() for x in _cell(r, 3).split(",") if x.strip())
        for label in row_labels:
            key = label.lower()
            if key in labels and labels[key] != email:
                raise ReferenceFormatError(
                    f"{where}: Register label {label!r} is also given to {labels[key]}"
                )
            if key in {"house", "auto renew", "cust success", "legacy"}:
                raise ReferenceFormatError(f"{where}: {label!r} is a House label, not a person")
            labels[key] = email
        last = _cell(r, 5)
        staff.append(
            StaffMember(
                name=name,
                email=email,
                job_title=_cell(r, 2) or None,
                register_labels=row_labels,
                xero_group=_cell(r, 4) or None,
                last_working_day=_date(last, where) if last else None,
            )
        )
    return staff


def parse_moves(rows: Sequence[Sequence[str]]) -> list[AccountMove]:
    moves: list[AccountMove] = []
    for n, r in _body(rows, MOVES_TAB, MOVES_HEADERS):
        where = f"{MOVES_TAB} row {n}"
        to = _cell(r, 1)
        reason = _cell(r, 3)
        if not reason:
            raise ReferenceFormatError(f"{where}: Reason is required (who decided and why)")
        moves.append(
            AccountMove(
                row=n,
                from_email=_email(_cell(r, 0), where),
                to=HOUSE if to.lower() == HOUSE.lower() else _email(to, where),
                effective=_date(_cell(r, 2), where),
                reason=reason,
            )
        )
    return moves


def staff_master_data(staff: Sequence[StaffMember]) -> MasterDataIn:
    """Employees, Register label mappings and Xero owner groups, in the master-data load format."""
    return MasterDataIn(
        employees=tuple(EmployeeIn(email=s.email, full_name=s.name, job_title=s.job_title) for s in staff),
        register_owner_aliases=tuple(
            RegisterOwnerAliasIn(register_label=label, owner_email=s.email)
            for s in staff
            for label in s.register_labels
        ),
        xero_owner_groups=tuple(
            XeroOwnerGroupIn(group_name=s.xero_group, owner_email=s.email) for s in staff if s.xero_group
        ),
    )
