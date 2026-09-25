"""reference data

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
