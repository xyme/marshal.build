"""Composable agents R1 (composable-agents spec, FSD §13.5R):
projects.composition — the declared dependency graph.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("composition", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "composition")
