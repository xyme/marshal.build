"""sprint12: deployment policies + lease budgets

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-27 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("deployment_policies", JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column("leases", sa.Column("budget_usd", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("leases", "budget_usd")
    op.drop_column("platform_settings", "deployment_policies")
