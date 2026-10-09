"""customer payment terms

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
