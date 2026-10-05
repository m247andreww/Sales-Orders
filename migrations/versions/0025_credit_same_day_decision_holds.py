"""credit same day decision holds

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
