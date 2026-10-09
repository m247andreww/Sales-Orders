"""Xero contact groups: who owns each customer account (CFO, 2026-09-25).

The account owner is recorded in Xero as a contact group (one group per salesperson, e.g.
"a. <first name>"). This module reads the groups and their contacts, either live from the Xero
Accounting API (a Xero custom connection, machine-to-machine, no browser login) or from a saved
API response. Both use Xero's own JSON shape, so a saved file and a live call are parsed by the
same code.

    GET https://api.xero.com/api.xro/2.0/ContactGroups          -> the groups
    GET https://api.xero.com/api.xro/2.0/ContactGroups/{id}     -> one group with its Contacts

Deleted groups are ignored. A response that does not look like Xero's is rejected, never guessed.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

API_BASE = "https://api.xero.com/api.xro/2.0"
TOKEN_URL = "https://identity.xero.com/connect/token"  # noqa: S105 - an endpoint, not a secret
DEFAULT_SCOPE = "accounting.contacts.read"

# Takes a prepared request, returns the response body. Injected so tests never call Xero.
Transport = Callable[[urllib.request.Request], bytes]


class XeroFormatError(ValueError):
    """The response does not have Xero's ContactGroups layout: nothing is loaded."""


@dataclass(frozen=True)
class XeroContact:
    contact_id: UUID
    name: str | None


@dataclass(frozen=True)
class XeroContactGroup:
    group_id: UUID
    name: str
    contacts: tuple[XeroContact, ...]


def _uuid(value: Any, what: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise XeroFormatError(f"{what} is not a Xero id: {value!r}") from exc


def _contact(c: Any, group: str) -> XeroContact:
    if not isinstance(c, Mapping) or "ContactID" not in c:
        raise XeroFormatError(f"group {group!r}: contact without ContactID: {c!r}")
    name = c.get("Name")
    return XeroContact(_uuid(c["ContactID"], f"a contact in {group!r}"), str(name) if name else None)


def parse_contact_groups(document: Mapping[str, Any]) -> list[XeroContactGroup]:
    """Parse a Xero ContactGroups response (groups may carry their Contacts). Active groups only."""
    groups = document.get("ContactGroups")
    if not isinstance(groups, list):
        raise XeroFormatError("expected a Xero response with a 'ContactGroups' list")
    parsed: list[XeroContactGroup] = []
    for g in groups:
        if not isinstance(g, Mapping) or "ContactGroupID" not in g or not g.get("Name"):
            raise XeroFormatError(f"contact group without ContactGroupID/Name: {g!r}")
        if str(g.get("Status", "ACTIVE")).upper() != "ACTIVE":
            continue
        contacts = g.get("Contacts", [])
        if not isinstance(contacts, list):
            raise XeroFormatError(f"group {g['Name']!r}: 'Contacts' is not a list")
        parsed.append(
            XeroContactGroup(
                group_id=_uuid(g["ContactGroupID"], f"group {g['Name']!r}"),
                name=str(g["Name"]).strip(),
                contacts=tuple(_contact(c, str(g["Name"])) for c in contacts),
            )
        )
    return parsed


def _urlopen(request: urllib.request.Request) -> bytes:
    if not request.full_url.startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS Xero URL: {request.full_url}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https enforced above
        body: bytes = response.read()
        return body


@dataclass(frozen=True)
class XeroCredentials:
    """A Xero custom connection (Xero Developer portal -> New app -> Custom connection)."""

    client_id: str
    client_secret: str
    scope: str = DEFAULT_SCOPE
    tenant_id: str | None = None  # not needed for a custom connection (it is tied to one organisation)


def _access_token(creds: XeroCredentials, transport: Transport) -> str:
    basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": creds.scope}).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    token = json.loads(transport(request)).get("access_token")
    if not token:
        raise XeroFormatError("Xero did not return an access token")
    return str(token)


def fetch_contact_groups(
    creds: XeroCredentials, transport: Transport = _urlopen
) -> tuple[list[XeroContactGroup], dict[str, Any]]:
    """Read every active contact group, with its contacts, live from Xero.

    Returns the parsed groups and the combined raw response (kept as evidence of what Xero said).
    """
    headers = {"Authorization": f"Bearer {_access_token(creds, transport)}", "Accept": "application/json"}
    if creds.tenant_id:
        headers["xero-tenant-id"] = creds.tenant_id

    def get(path: str) -> dict[str, Any]:
        request = urllib.request.Request(f"{API_BASE}/{path}", headers=headers)  # noqa: S310 - https base
        body: dict[str, Any] = json.loads(transport(request))
        return body

    detailed: list[Any] = []
    for group in parse_contact_groups(get("ContactGroups")):
        detailed.extend(get(f"ContactGroups/{group.group_id}").get("ContactGroups", []))
    combined = {"ContactGroups": detailed}
    return parse_contact_groups(combined), combined


# ---------------------------------------------------------------------------- contact directory
# The daily credit run keeps a copy of the Xero contact list (migration 0018) to suggest the Xero contact
# and company number for a customer that is in the ARR file but not yet credit-monitored.


@dataclass(frozen=True)
class XeroDirectoryContact:
    contact_id: UUID
    name: str
    company_number: str | None
    is_customer: bool
    is_supplier: bool
    status: str
    sales_terms_days: int | None = None  # the contact's own sales payment terms; None = Xero's default
    sales_terms_type: str | None = None


SALES_TERMS_TYPES = ("DAYSAFTERBILLDATE", "DAYSAFTERBILLMONTH", "OFCURRENTMONTH", "OFFOLLOWINGMONTH")


def _sales_terms(c: Mapping[str, Any]) -> tuple[int | None, str | None]:
    sales = (c.get("PaymentTerms") or {}).get("Sales") if isinstance(c.get("PaymentTerms"), Mapping) else None
    if not isinstance(sales, Mapping) or sales.get("Type") is None:
        return None, None
    day, kind = sales.get("Day"), str(sales.get("Type"))
    if kind not in SALES_TERMS_TYPES or isinstance(day, bool) or not isinstance(day, int):
        raise XeroFormatError(f"unexpected sales payment terms on {c.get('Name')!r}: {sales!r}")
    return day, kind


def parse_contacts(document: Mapping[str, Any]) -> list[XeroDirectoryContact]:
    """Parse one page of a Xero Contacts response."""
    contacts = document.get("Contacts")
    if not isinstance(contacts, list):
        raise XeroFormatError("the response has no Contacts list")
    out = []
    for c in contacts:
        if not isinstance(c, Mapping) or "ContactID" not in c or not str(c.get("Name") or "").strip():
            raise XeroFormatError(f"contact without ContactID or Name: {c!r}")
        number = str(c.get("CompanyNumber") or "").strip().upper() or None
        terms_days, terms_type = _sales_terms(c)
        out.append(
            XeroDirectoryContact(
                contact_id=_uuid(c["ContactID"], "a contact"),
                name=str(c["Name"]).strip(),
                company_number=number,
                is_customer=bool(c.get("IsCustomer")),
                is_supplier=bool(c.get("IsSupplier")),
                status=str(c.get("ContactStatus") or "ACTIVE"),
                sales_terms_days=terms_days,
                sales_terms_type=terms_type,
            )
        )
    return out


def fetch_contacts(creds: XeroCredentials, transport: Transport = _urlopen) -> list[XeroDirectoryContact]:
    """Every contact in the organisation (Xero pages them 100 at a time)."""
    headers = {"Authorization": f"Bearer {_access_token(creds, transport)}", "Accept": "application/json"}
    if creds.tenant_id:
        headers["xero-tenant-id"] = creds.tenant_id
    out: list[XeroDirectoryContact] = []
    page = 1
    while True:
        request = urllib.request.Request(f"{API_BASE}/Contacts?page={page}", headers=headers)  # noqa: S310 - https base
        batch = parse_contacts(json.loads(transport(request)))
        out.extend(batch)
        if len(batch) < XERO_PAGE_SIZE:
            return out
        page += 1


XERO_PAGE_SIZE = 100


# ---------------------------------------------------------------------------- credit snapshots
# Filing a credit assessment on the customer's Xero contact (migration 0014). Needs the custom
# connection to have the scopes "accounting.contacts accounting.attachments" (write).
# Xero's public API has no credit-limit field on a contact: the limit is recorded in a history note
# on the contact and held in sales.customer_credit_limit.
CREDIT_SCOPE = "accounting.contacts accounting.attachments"


class XeroClient:
    """A token-caching client for the write calls the credit routine makes."""

    def __init__(self, creds: XeroCredentials, transport: Transport = _urlopen) -> None:
        self._creds = creds
        self._transport = transport
        self._token: str | None = None

    def _headers(self, extra: Mapping[str, str]) -> dict[str, str]:
        if self._token is None:
            self._token = _access_token(self._creds, self._transport)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json", **extra}
        if self._creds.tenant_id:
            headers["xero-tenant-id"] = self._creds.tenant_id
        return headers

    def attach_to_contact(
        self, contact_id: UUID, file_name: str, content: bytes, idempotency_key: str
    ) -> str:
        """Upload a file to the contact's Files; returns Xero's AttachmentID."""
        request = urllib.request.Request(  # noqa: S310 - https base
            f"{API_BASE}/Contacts/{contact_id}/Attachments/{urllib.parse.quote(file_name)}",
            data=content,
            headers=self._headers({"Content-Type": "application/pdf", "Idempotency-Key": idempotency_key}),
            method="PUT",
        )
        body = json.loads(self._transport(request))
        attachments = body.get("Attachments") if isinstance(body, Mapping) else None
        if not isinstance(attachments, list) or not attachments or "AttachmentID" not in attachments[0]:
            raise XeroFormatError("Xero did not confirm the attachment")
        return str(attachments[0]["AttachmentID"])

    def set_company_number(self, contact_id: UUID, company_number: str) -> None:
        """Set the contact's Company number (only that field is sent, so nothing else changes)."""
        request = urllib.request.Request(  # noqa: S310 - https base
            f"{API_BASE}/Contacts/{contact_id}",
            data=json.dumps(
                {"Contacts": [{"ContactID": str(contact_id), "CompanyNumber": company_number}]}
            ).encode(),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        body = json.loads(self._transport(request))
        contacts = body.get("Contacts") if isinstance(body, Mapping) else None
        if (
            not isinstance(contacts, list)
            or not contacts
            or contacts[0].get("CompanyNumber") != company_number
        ):
            raise XeroFormatError("Xero did not confirm the company number")

    def set_sales_terms(self, contact_id: UUID, days: int, kind: str) -> None:
        """Set the contact's sales payment terms (only that field is sent, so nothing else changes)."""
        if kind not in SALES_TERMS_TYPES:
            raise XeroFormatError(f"unknown Xero payment terms type {kind!r}")
        request = urllib.request.Request(  # noqa: S310 - https base
            f"{API_BASE}/Contacts/{contact_id}",
            data=json.dumps(
                {
                    "Contacts": [
                        {"ContactID": str(contact_id), "PaymentTerms": {"Sales": {"Day": days, "Type": kind}}}
                    ]
                }
            ).encode(),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        body = json.loads(self._transport(request))
        contacts = body.get("Contacts") if isinstance(body, Mapping) else None
        if not isinstance(contacts, list) or not contacts or _sales_terms(contacts[0]) != (days, kind):
            raise XeroFormatError("Xero did not confirm the payment terms")

    def add_contact_note(self, contact_id: UUID, details: str, idempotency_key: str) -> None:
        """Add a line to the contact's History and notes."""
        request = urllib.request.Request(  # noqa: S310 - https base
            f"{API_BASE}/Contacts/{contact_id}/History",
            data=json.dumps({"HistoryRecords": [{"Details": details[:2500]}]}).encode(),
            headers=self._headers({"Content-Type": "application/json", "Idempotency-Key": idempotency_key}),
            method="PUT",
        )
        self._transport(request)
