"""Create speakers table.

Revision ID: 20260107_0001
Revises:
Create Date: 2026-01-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260107_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "speakers",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("meta", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("speakers")
