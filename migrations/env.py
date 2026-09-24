"""Alembic environment. Migrations are plain SQL files in migrations/sql/."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from sales_orders.config import sqlalchemy_url

config = context.config


def run_migrations_offline() -> None:
    context.configure(url=sqlalchemy_url(), literal_binds=True, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            transactional_ddl=True,
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
