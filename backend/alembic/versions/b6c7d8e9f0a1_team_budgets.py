"""Add per-team monthly budgets for the cost dashboard.

Visibility feature (5 Sep 2026): budget-vs-spend per team with threshold
coloring. Deliberately NOT enforced at the model seam — user, project and
platform caps remain the enforcement instruments; a team budget is an
accountability number.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: str | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "teams",
        sa.Column("budget_usd", sa.Numeric(10, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("teams", "budget_usd")
