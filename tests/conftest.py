"""Test harness.

Tests run against a real PostgreSQL (never SQLite or mocks): a throwaway database is
created per test session, migrated down-and-up to prove the migrations are reversible,
and each test runs inside a transaction that is rolled back afterwards.

Set SALES_ORDERS_TEST_ADMIN_URL to a role that may CREATE DATABASE
(default: postgresql://postgres:postgres@localhost:5432/postgres).
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.rows import dict_row

from sales_orders.db import Connection, set_actor
from sales_orders.models import MasterDataIn, OrderSubmissionIn
from sales_orders.service import OrderResult, create_sales_order, load_master_data

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
ADMIN_URL = os.environ.get(
    "SALES_ORDERS_TEST_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)


def _db_url(name: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


def _alembic(url: str) -> Config:
    os.environ["SALES_ORDERS_DATABASE_URL"] = url
    return Config(str(ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    name = f"sales_orders_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = _db_url(name)
    try:
        cfg = _alembic(url)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")  # prove every migration reverses cleanly
        command.upgrade(cfg, "head")
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def conn(database_url: str) -> Iterator[Connection]:
    """A connection whose work is always rolled back, so tests are independent."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        tx = connection.transaction(force_rollback=True)
        with tx:
            set_actor(connection, "pytest")
            yield connection


def fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def master_data(conn: Connection) -> MasterDataIn:
    data = MasterDataIn.model_validate(fixture_json("test_master_data.json"))
    load_master_data(conn, data)
    return data


@pytest.fixture
def order_json() -> dict[str, Any]:
    """A fresh, mutable copy of the test order submission."""
    return copy.deepcopy(fixture_json("test_order.json"))


def as_new_submission(order: dict[str, Any]) -> OrderSubmissionIn:
    """Validate a (possibly modified) order dict as a distinct email so it creates a new order."""
    order = copy.deepcopy(order)
    order["source_email"]["internet_message_id"] = f"<{uuid.uuid4()}@example.com>"
    return OrderSubmissionIn.model_validate(order)


def load(conn: Connection, order: dict[str, Any]) -> OrderResult:
    return create_sales_order(conn, as_new_submission(order))


def savepoint_rejects(conn: Connection, statement: str | sql.Composable, params: tuple[Any, ...] = ()) -> str:
    """Run SQL expected to fail; return the database error message. Keeps the outer tx usable."""
    query = sql.SQL(statement) if isinstance(statement, str) else statement  # type: ignore[arg-type]
    with pytest.raises(psycopg.Error) as info, conn.transaction():
        conn.execute(query, params or None)
    return str(info.value.diag.message_primary)
