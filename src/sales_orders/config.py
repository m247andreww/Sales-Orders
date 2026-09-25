"""Runtime configuration. All settings come from the environment; nothing is hard-coded."""

from __future__ import annotations

import os

DATABASE_URL_ENV = "SALES_ORDERS_DATABASE_URL"


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


def database_url() -> str:
    """libpq connection URL, e.g. postgresql://user:pass@host:5432/sales_orders."""
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if url:
        return url
    if os.environ.get("PGHOST", "").strip():
        # Standard libpq variables (PGHOST, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE): the Azure job
        # receives its password from Key Vault this way, so it never appears inside a URL.
        return ""
    raise ConfigurationError(f"{DATABASE_URL_ENV} (or PGHOST and friends) is not set (see .env.example)")


def sqlalchemy_url() -> str:
    """The same URL with the psycopg 3 driver selected, for Alembic/SQLAlchemy."""
    url = database_url()
    if not url:
        raise ConfigurationError(f"migrations need {DATABASE_URL_ENV} set to a full connection URL")
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url
