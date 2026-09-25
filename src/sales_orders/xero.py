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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sales_orders.http import Transport, https_transport

API_BASE = "https://api.xero.com/api.xro/2.0"
TOKEN_URL = "https://identity.xero.com/connect/token"  # noqa: S105 - an endpoint, not a secret
# Contacts (customers, owner groups) and settings (chart of accounts). Read-only.
DEFAULT_SCOPE = "accounting.contacts.read accounting.settings.read"


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


class XeroClient:
    """Authenticated, read-only access to the Xero Accounting API. The access token is fetched once."""

    def __init__(self, creds: XeroCredentials, transport: Transport = https_transport) -> None:
        self._creds = creds
        self._transport = transport
        self._token: str | None = None

    def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        if self._token is None:
            self._token = _access_token(self._creds, self._transport)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self._creds.tenant_id:
            headers["xero-tenant-id"] = self._creds.tenant_id
        query = f"?{urllib.parse.urlencode(dict(params))}" if params else ""
        request = urllib.request.Request(f"{API_BASE}/{path}{query}", headers=headers)  # noqa: S310 - https base
        body: dict[str, Any] = json.loads(self._transport(request))
        return body


def fetch_contact_groups(
    creds: XeroCredentials, transport: Transport = https_transport
) -> tuple[list[XeroContactGroup], dict[str, Any]]:
    """Read every active contact group, with its contacts, live from Xero.

    Returns the parsed groups and the combined raw response (kept as evidence of what Xero said).
    """
    return read_contact_groups(XeroClient(creds, transport))


def read_contact_groups(client: XeroClient) -> tuple[list[XeroContactGroup], dict[str, Any]]:
    """As fetch_contact_groups, through an existing client (one sign-in for the whole nightly run)."""
    detailed: list[Any] = []
    for group in parse_contact_groups(client.get("ContactGroups")):
        detailed.extend(client.get(f"ContactGroups/{group.group_id}").get("ContactGroups", []))
    combined = {"ContactGroups": detailed}
    return parse_contact_groups(combined), combined


# ------------------------------------------------------------------ customers and chart of accounts


@dataclass(frozen=True)
class XeroCustomer:
    contact_id: UUID
    name: str
    archived: bool


@dataclass(frozen=True)
class XeroAccount:
    account_id: UUID
    code: str | None
    name: str
    account_class: str
    account_type: str
    tax_type: str | None
    active: bool


def parse_customers(document: Mapping[str, Any]) -> list[XeroCustomer]:
    """Parse a Xero Contacts response; only contacts Xero marks as customers are returned."""
    contacts = document.get("Contacts")
    if not isinstance(contacts, list):
        raise XeroFormatError("expected a Xero response with a 'Contacts' list")
    parsed: list[XeroCustomer] = []
    for c in contacts:
        if not isinstance(c, Mapping) or "ContactID" not in c or not str(c.get("Name") or "").strip():
            raise XeroFormatError(f"contact without ContactID/Name: {c!r}")
        if c.get("IsCustomer") is False:
            continue
        parsed.append(
            XeroCustomer(
                contact_id=_uuid(c["ContactID"], f"contact {c['Name']!r}"),
                name=" ".join(str(c["Name"]).split()),
                archived=str(c.get("ContactStatus", "ACTIVE")).upper() == "ARCHIVED",
            )
        )
    return parsed


def parse_accounts(document: Mapping[str, Any]) -> list[XeroAccount]:
    """Parse a Xero Accounts (chart of accounts) response."""
    accounts = document.get("Accounts")
    if not isinstance(accounts, list):
        raise XeroFormatError("expected a Xero response with an 'Accounts' list")
    parsed: list[XeroAccount] = []
    for a in accounts:
        if not isinstance(a, Mapping) or "AccountID" not in a or not a.get("Name") or not a.get("Class"):
            raise XeroFormatError(f"account without AccountID/Name/Class: {a!r}")
        parsed.append(
            XeroAccount(
                account_id=_uuid(a["AccountID"], f"account {a['Name']!r}"),
                code=str(a["Code"]).strip() if a.get("Code") else None,
                name=str(a["Name"]).strip(),
                account_class=str(a["Class"]).upper(),
                account_type=str(a.get("Type") or "").upper(),
                tax_type=str(a["TaxType"]) if a.get("TaxType") else None,
                active=str(a.get("Status", "ACTIVE")).upper() == "ACTIVE",
            )
        )
    return parsed


XERO_PAGE_SIZE = 100  # Xero returns contacts 100 to a page


def fetch_customers(client: XeroClient) -> list[XeroCustomer]:
    """Every Xero contact marked as a customer, archived ones included (so the database can follow)."""
    customers: list[XeroCustomer] = []
    page = 1
    while True:
        document = client.get(
            "Contacts",
            {
                "where": "IsCustomer==true",
                "summaryOnly": "true",
                "includeArchived": "true",
                "page": str(page),
            },
        )
        batch = parse_customers(document)
        customers.extend(batch)
        if len(document.get("Contacts", [])) < XERO_PAGE_SIZE:
            return customers
        page += 1


def fetch_accounts(client: XeroClient) -> list[XeroAccount]:
    return parse_accounts(client.get("Accounts"))
