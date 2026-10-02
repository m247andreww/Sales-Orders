"""Where the database lives between runs of the daily Claude job (ADR 0005, redesign 2026-10-02).

The daily job runs in a fresh cloud container that cannot reach the Azure PostgreSQL server, so
the database is carried between runs as a pg_dump in Managed247's own Azure Blob Storage (UK South,
versioning on: every saved state is kept). Each run restores the latest dump, works, and saves a new
one. Integrity and concurrency are checked, never assumed:

* the SHA-256 of every dump is stored with it (blob metadata) and verified on restore;
* a save is conditional on the blob not having changed since this run restored it (HTTP If-Match),
  so two runs can never silently overwrite each other's work.

Access is a container SAS URL (read/write on one container only), held in the environment's
credentials as SALES_ORDERS_STATE_URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BLOB_NAME = "sales_orders.dump"
LOCK_FILE = Path(os.environ.get("SALES_ORDERS_STATE_LOCK", Path.home() / ".sales_orders_state.json"))
Transport = Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]]


class StateStoreError(RuntimeError):
    """The saved state could not be read or written safely: nothing was changed."""


def _urlopen(request: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    if not request.full_url.startswith("https://"):
        raise StateStoreError("refusing a non-HTTPS state store URL")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - https enforced above
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


@dataclass(frozen=True)
class SavedState:
    content: bytes
    etag: str
    sha256: str


class BlobStateStore:
    def __init__(self, container_sas_url: str, transport: Transport = _urlopen) -> None:
        parts = urllib.parse.urlsplit(container_sas_url)
        if parts.scheme != "https" or not parts.query:
            raise StateStoreError("SALES_ORDERS_STATE_URL must be an https container URL with its SAS token")
        self._url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, f"{parts.path.rstrip('/')}/{BLOB_NAME}", parts.query, "")
        )
        self._transport = transport

    def load(self) -> SavedState | None:
        """The latest saved state, verified; None if nothing has been saved yet."""
        status, headers, body = self._transport(
            urllib.request.Request(  # noqa: S310 - https enforced in __init__
                self._url, headers={"x-ms-version": "2021-08-06"}, method="GET"
            )
        )
        if status == 404:  # noqa: PLR2004 - HTTP Not Found: first run
            return None
        if status != 200:  # noqa: PLR2004
            raise StateStoreError(f"state store read failed: HTTP {status}")
        expected = headers.get("x-ms-meta-sha256")
        actual = hashlib.sha256(body).hexdigest()
        if expected != actual:
            raise StateStoreError(
                f"saved state is corrupt: SHA-256 {actual} does not match the recorded {expected}"
            )
        return SavedState(body, headers.get("etag", ""), actual)

    def save(self, content: bytes, if_match: str | None) -> str:
        """Save a new state. if_match: the ETag restored from (None = first save ever). Returns the new ETag."""
        headers = {
            "x-ms-version": "2021-08-06",
            "x-ms-blob-type": "BlockBlob",
            "x-ms-meta-sha256": hashlib.sha256(content).hexdigest(),
            "Content-Type": "application/octet-stream",
        }
        headers["If-Match" if if_match else "If-None-Match"] = if_match or "*"
        status, response_headers, _ = self._transport(
            urllib.request.Request(  # noqa: S310 - https enforced in __init__
                self._url, data=content, headers=headers, method="PUT"
            )
        )
        if status in (409, 412):
            raise StateStoreError(
                "the saved state changed since this run restored it (another run saved first): not overwritten"
            )
        if status != 201:  # noqa: PLR2004 - Created
            raise StateStoreError(f"state store write failed: HTTP {status}")
        return response_headers.get("etag", "")


# ---------------------------------------------------------------------------- dump / restore


def _pg(args: list[str], database_url: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed PostgreSQL client binaries, no shell
        [*args, f"--dbname={database_url}"], input=stdin, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise StateStoreError(f"{args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def restore(store: BlobStateStore, database_url: str) -> str:
    """Restore the latest saved state into an EMPTY database. Returns a one-line description."""
    saved = store.load()
    if saved is None:
        LOCK_FILE.write_text(json.dumps({"etag": None}), encoding="utf-8")
        return "no saved state yet: starting from an empty database"
    _pg(
        ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"],
        database_url,
        saved.content,
    )
    LOCK_FILE.write_text(json.dumps({"etag": saved.etag, "sha256": saved.sha256}), encoding="utf-8")
    return f"restored saved state {saved.sha256[:12]} ({len(saved.content):,} bytes)"


def save(store: BlobStateStore, database_url: str) -> str:
    """Save the database as the new state, only if nobody saved since this run restored."""
    if not LOCK_FILE.exists():
        raise StateStoreError(
            "this run did not restore the saved state first (state-restore): refusing to overwrite it"
        )
    restored_from = json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("etag")
    dump = _pg(["pg_dump", "--format=custom", "--no-owner", "--no-privileges"], database_url)
    etag = store.save(dump, restored_from)
    LOCK_FILE.write_text(
        json.dumps({"etag": etag, "sha256": hashlib.sha256(dump).hexdigest()}), encoding="utf-8"
    )
    return f"saved state {hashlib.sha256(dump).hexdigest()[:12]} ({len(dump):,} bytes)"
