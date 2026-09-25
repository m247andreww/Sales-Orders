"""Runtime configuration. All settings come from the environment; nothing is hard-coded."""

from __future__ import annotations

import os

DATABASE_URL_ENV = "SALES_ORDERS_DATABASE_URL"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


def database_url() -> str:
    """libpq connection URL, e.g. postgresql://user:pass@host:5432/sales_orders."""
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url:
        raise ConfigurationError(f"{DATABASE_URL_ENV} is not set (see .env.example)")
    return url


def sqlalchemy_url() -> str:
    """The same URL with the psycopg 3 driver selected, for Alembic/SQLAlchemy."""
    url = database_url()
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url
