"""initial schema

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
