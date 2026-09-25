"""Read-only access to Google Sheets (the AW SOs Register and the Sales Orders - Reference sheet).

The nightly job signs in as a Google service account (a robot account that has been given Viewer
access to the two sheets) using its key, held in Azure Key Vault. It reads cell values exactly as
they are displayed, which is what the Register parser expects (e.g. " £ 5,400 ", "5 Jan 24").
"""

from __future__ import annotations

import base64
import csv
import io
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sales_orders.http import Transport, https_transport

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class GoogleSheetsError(RuntimeError):
    """The key is unusable, or Google refused or returned something unexpected: nothing is loaded."""


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    private_key_pem: str
    token_uri: str

    @classmethod
    def from_json(cls, text: str) -> ServiceAccount:
        """Parse a Google service-account key file (the JSON Google issues)."""
        try:
            info = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GoogleSheetsError("the Google key is not valid JSON") from exc
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise GoogleSheetsError("the Google key is not a service-account key")
        missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
        if missing:
            raise GoogleSheetsError(f"the Google key is missing: {', '.join(missing)}")
        return cls(info["client_email"], info["private_key"], info["token_uri"])


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_assertion(account: ServiceAccount, now: float) -> str:
    """The signed request Google exchanges for an access token (RFC 7523, RS256)."""
    key = serialization.load_pem_private_key(account.private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GoogleSheetsError("the Google key is not an RSA key")
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": account.client_email,
        "scope": READONLY_SCOPE,
        "aud": account.token_uri,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


class SheetsClient:
    """Reads cell values from Google Sheets as the service account."""

    def __init__(
        self,
        account: ServiceAccount,
        transport: Transport = https_transport,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._account = account
        self._transport = transport
        self._clock = clock
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": signed_assertion(self._account, self._clock()),
                }
            ).encode()
            request = urllib.request.Request(  # noqa: S310 - the token URI from Google's own key file
                self._account.token_uri,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            token = _json(self._transport(request)).get("access_token")
            if not token:
                raise GoogleSheetsError("Google did not return an access token")
            self._token = str(token)
        return self._token

    def values(self, spreadsheet_id: str, sheet_range: str) -> list[list[str]]:
        """Displayed values of a range (e.g. a whole tab: "Register"), row by row."""
        url = (
            f"{SHEETS_API}/{urllib.parse.quote(spreadsheet_id, safe='')}/values/"
            f"{urllib.parse.quote(sheet_range, safe='')}?valueRenderOption=FORMATTED_VALUE&majorDimension=ROWS"
        )
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._access_token()}"})  # noqa: S310 - https base
        rows = _json(self._transport(request)).get("values", [])
        if not isinstance(rows, list):
            raise GoogleSheetsError(f"unexpected response for {sheet_range!r}")
        return [[str(cell) for cell in row] for row in rows]


def _json(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsError("Google returned something that is not JSON") from exc
    if not isinstance(parsed, dict):
        raise GoogleSheetsError("Google returned an unexpected response")
    return parsed


def rows_as_csv(rows: Sequence[Sequence[str]]) -> io.StringIO:
    """Rows as CSV text, every row padded to the same width.

    Google leaves out empty cells at the end of a row; the CSV export the Register parser was
    built on does not, so rows are padded to match it.
    """
    width = max((len(r) for r in rows), default=0)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(list(row) + [""] * (width - len(row)))
    buffer.seek(0)
    return buffer
