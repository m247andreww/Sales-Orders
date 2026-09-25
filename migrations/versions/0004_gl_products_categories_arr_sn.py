"""gl products categories arr sn

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from pathlib import Path

from migrations_support import run_sql_file

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.up.sql")


def downgrade() -> None:
    run_sql_file(f"{Path(__file__).stem}.down.sql")
