"""Helpers shared by migration revisions."""

from __future__ import annotations

from pathlib import Path

from alembic import op

SQL_DIR = Path(__file__).resolve().parent / "sql"


def run_sql_file(name: str) -> None:
    """Execute a reviewed SQL file verbatim inside Alembic's transaction.

    The text goes straight to psycopg with params=None, so '%' and ':' in the SQL are
    literal (SQLAlchemy's exec_driver_sql would pass an empty tuple and psycopg would
    then treat '%' as a placeholder).
    """
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    driver_connection = op.get_bind().connection.driver_connection
    if driver_connection is None:
        raise RuntimeError("no DBAPI connection available for migration")
    driver_connection.execute(sql)
