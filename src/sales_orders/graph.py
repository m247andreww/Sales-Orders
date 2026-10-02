"""Microsoft Graph (Microsoft 365): the mailbox and SharePoint access the credit routine needs.

App-only access (an Entra ID app registration with a client secret; no browser sign-in), limited by
an Exchange application access policy to the one mailbox it serves. Permissions:
  Mail.ReadWrite  - read bureau alerts, copy them into Debt & Credit folders
  Mail.Send       - send the daily credit summary
  Sites.Selected  - read the ARR file (granted on the Finance SharePoint site only)

Like xero.py, every request goes through an injected transport, so tests never call Microsoft.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

GRAPH = "https://graph.microsoft.com/v1.0"
Transport = Callable[[urllib.request.Request], bytes]


class GraphError(RuntimeError):
    """Microsoft Graph refused or returned something unexpected: nothing was changed."""


@dataclass(frozen=True)
class GraphCredentials:
    tenant_id: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class MailMessage:
    graph_id: str
    internet_message_id: str
    sender: str
    subject: str
    received_at: datetime
    html: str


@dataclass(frozen=True)
class MailFolder:
    folder_id: str
    name: str
    path: tuple[str, ...]  # names from the mailbox root, e.g. ("Inbox", "Clients", "Acme", "Debt & Credit")


def _urlopen(request: urllib.request.Request) -> bytes:
    if not request.full_url.startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS URL: {request.full_url}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https enforced above
        body: bytes = response.read()
        return body


class GraphClient:
    def __init__(self, creds: GraphCredentials, transport: Transport = _urlopen) -> None:
        self._creds = creds
        self._transport = transport
        self._token: str | None = None

    def _bearer(self) -> str:
        if self._token is None:
            request = urllib.request.Request(
                f"https://login.microsoftonline.com/{urllib.parse.quote(self._creds.tenant_id)}/oauth2/v2.0/token",
                data=urllib.parse.urlencode(
                    {
                        "grant_type": "client_credentials",
                        "client_id": self._creds.client_id,
                        "client_secret": self._creds.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    }
                ).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            token = json.loads(self._transport(request)).get("access_token")
            if not token:
                raise GraphError("Microsoft did not return an access token")
            self._token = str(token)
        return self._token

    def _call(self, method: str, url: str, body: dict[str, Any] | None = None, raw: bool = False) -> Any:
        full = url if url.startswith("https://") else f"{GRAPH}{url}"
        request = urllib.request.Request(  # noqa: S310 - https only (checked in _urlopen)
            full,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Prefer": 'outlook.body-content-type="html"',
            },
            method=method,
        )
        data = self._transport(request)
        if raw:
            return data
        return json.loads(data) if data else {}

    def _pages(self, url: str) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            page = self._call("GET", next_url)
            yield from page.get("value", [])
            next_url = page.get("@odata.nextLink")

    # ------------------------------------------------------------------ mail
    def messages_from(self, mailbox: str, sender: str, since: datetime) -> list[MailMessage]:
        """Every message from `sender` received at or after `since`, in any folder, oldest first."""
        query = urllib.parse.urlencode(
            {
                "$filter": f"from/emailAddress/address eq '{sender}' and receivedDateTime ge "
                f"{since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "$select": "id,internetMessageId,subject,receivedDateTime,body,from,parentFolderId",
                "$top": "50",
            }
        )
        seen: dict[str, MailMessage] = {}
        for m in self._pages(f"/users/{urllib.parse.quote(mailbox)}/messages?{query}"):
            imid = m.get("internetMessageId")
            if not imid or imid in seen:  # the CFO's filed copies share the Message-ID: keep one
                continue
            seen[imid] = MailMessage(
                graph_id=str(m["id"]),
                internet_message_id=str(imid),
                sender=str(m.get("from", {}).get("emailAddress", {}).get("address", "")).lower(),
                subject=str(m.get("subject") or ""),
                received_at=datetime.fromisoformat(str(m["receivedDateTime"]).replace("Z", "+00:00")),
                html=str(m.get("body", {}).get("content") or ""),
            )
        return sorted(seen.values(), key=lambda x: x.received_at)

    def folders(self, mailbox: str) -> list[MailFolder]:
        """Every mail folder in the mailbox, with its path."""
        found: list[MailFolder] = []

        def walk(url: str, path: tuple[str, ...]) -> None:
            for f in self._pages(url):
                here = (*path, str(f["displayName"]))
                found.append(MailFolder(str(f["id"]), str(f["displayName"]), here))
                if f.get("childFolderCount"):
                    walk(
                        f"/users/{urllib.parse.quote(mailbox)}/mailFolders/{f['id']}/childFolders?$top=100",
                        here,
                    )

        walk(f"/users/{urllib.parse.quote(mailbox)}/mailFolders?$top=100", ())
        return found

    def copy_message(self, mailbox: str, graph_id: str, folder_id: str) -> str:
        result = self._call(
            "POST",
            f"/users/{urllib.parse.quote(mailbox)}/messages/{urllib.parse.quote(graph_id)}/copy",
            {"destinationId": folder_id},
        )
        if not result.get("id"):
            raise GraphError("copy returned no message id")
        return str(result["id"])

    def send_mail(self, mailbox: str, to: str, subject: str, html: str) -> None:
        self._call(
            "POST",
            f"/users/{urllib.parse.quote(mailbox)}/sendMail",
            {
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": html},
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
                "saveToSentItems": True,
            },
        )

    # ------------------------------------------------------------------ files
    def download_shared_file(self, sharing_url: str) -> tuple[bytes, datetime | None, str]:
        """Content, last-modified time and name of a SharePoint/OneDrive file given its web URL."""
        token = "u!" + base64.urlsafe_b64encode(sharing_url.encode()).decode().rstrip("=")
        item = self._call("GET", f"/shares/{token}/driveItem?$select=name,lastModifiedDateTime")
        content = self._call("GET", f"/shares/{token}/driveItem/content", raw=True)
        modified = item.get("lastModifiedDateTime")
        return (
            bytes(content),
            datetime.fromisoformat(str(modified).replace("Z", "+00:00")) if modified else None,
            str(item.get("name") or "ARR file"),
        )
