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
