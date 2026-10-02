"""credit alert continuity

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
