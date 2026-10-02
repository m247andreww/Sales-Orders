"""The daily job's database is carried between runs in Azure Blob Storage: verified, never overwritten blind."""

from __future__ import annotations

import hashlib
import urllib.request
import uuid
from pathlib import Path

import psycopg
import pytest
from conftest import ADMIN_URL, _db_url
from psycopg import sql

from sales_orders import state_store
from sales_orders.state_store import BlobStateStore, StateStoreError

SAS = "https://example.blob.core.windows.net/credit-state?sv=2022-11-02&sig=test"


class FakeBlob:
    """Azure Blob semantics that matter here: ETag, If-Match / If-None-Match, sha256 metadata."""

    def __init__(self) -> None:
        self.content: bytes | None = None
        self.meta: str | None = None
        self.version = 0
        self.urls: list[str] = []

    def __call__(self, request: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        self.urls.append(request.full_url)
        etag = f'"v{self.version}"'
        if request.get_method() == "GET":
            if self.content is None:
                return 404, {}, b""
            return 200, {"etag": etag, "x-ms-meta-sha256": self.meta or ""}, self.content
        if_match, if_none = request.get_header("If-match"), request.get_header("If-none-match")
        if (if_none == "*" and self.content is not None) or (if_match and if_match != etag):
            return 412, {}, b""
        data = request.data
        assert isinstance(data, bytes)
        self.content, self.meta, self.version = data, request.get_header("X-ms-meta-sha256"), self.version + 1
        return 201, {"etag": f'"v{self.version}"'}, b""


def test_blob_url_and_integrity() -> None:
    blob = FakeBlob()
    store = BlobStateStore(SAS, blob)
    assert store.load() is None
    etag = store.save(b"dump-1", None)
    assert (
        blob.urls[-1]
        == "https://example.blob.core.windows.net/credit-state/sales_orders.dump?sv=2022-11-02&sig=test"
    )
    saved = store.load()
    assert saved is not None and (saved.content, saved.etag) == (b"dump-1", etag)
    blob.content = b"tampered"
    with pytest.raises(StateStoreError, match="corrupt"):
        store.load()


def test_a_save_never_overwrites_a_newer_state() -> None:
    blob = FakeBlob()
    store = BlobStateStore(SAS, blob)
    first = store.save(b"run A", None)
    store.save(b"run B", first)  # B restored from `first` and saved
    with pytest.raises(StateStoreError, match="another run saved first"):
        store.save(b"run C", first)  # C also restored from `first`: refused
    with pytest.raises(StateStoreError, match="another run saved first"):
        store.save(b"run D", None)  # a "first ever" save when a state exists: refused
    assert blob.content == b"run B"


def test_only_https_sas_urls() -> None:
    with pytest.raises(StateStoreError, match="https container URL"):
        BlobStateStore("http://example.blob.core.windows.net/c?sig=x")
    with pytest.raises(StateStoreError, match="https container URL"):
        BlobStateStore("https://example.blob.core.windows.net/c")  # no SAS token


def test_dump_restore_round_trip(database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_store, "LOCK_FILE", tmp_path / "lock.json")
    blob = FakeBlob()
    store = BlobStateStore(SAS, blob)
    with pytest.raises(StateStoreError, match="did not restore"):
        state_store.save(store, database_url)
    assert "no saved state" in state_store.restore(store, database_url)
    assert state_store.save(store, database_url).startswith("saved state ")
    assert blob.meta == hashlib.sha256(blob.content or b"").hexdigest()

    name = f"sales_orders_restore_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        state_store.restore(store, _db_url(name))
        with psycopg.connect(database_url) as source, psycopg.connect(_db_url(name)) as restored:
            query = "SELECT string_agg(rule_code, ',' ORDER BY rule_code) FROM sales.credit_exception_rule"
            original, copy = source.execute(query).fetchone(), restored.execute(query).fetchone()
            assert original is not None and original[0]  # the source really has rules to compare
            assert copy == original
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
