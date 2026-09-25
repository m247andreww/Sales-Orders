"""Database connections.

Every unit of work runs in one transaction with the acting user recorded, so the audit
trail in audit.change_log always says who did what.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

from sales_orders.config import database_url

Connection = psycopg.Connection[DictRow]


@contextmanager
def unit_of_work(actor: str, url: str | None = None) -> Iterator[Connection]:
    """Open a connection, run one transaction as `actor`, commit on success, roll back on error."""
    if not actor.strip():
        raise ValueError("actor is required for the audit trail")
    with psycopg.connect(url or database_url(), row_factory=dict_row) as conn, conn.transaction():
        set_actor(conn, actor)
        yield conn


def set_actor(conn: psycopg.Connection[Any], actor: str) -> None:
    """Record the acting user for the current transaction (read by audit.current_actor())."""
    conn.execute("SELECT set_config('app.current_user', %s, true)", (actor,))
