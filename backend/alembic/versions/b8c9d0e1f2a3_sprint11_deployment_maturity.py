"""sprint11: deployment health, in-place updates, expiry

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("health", sa.String(length=12), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "deployments", sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "deployments", sa.Column("health_path", sa.String(length=256), nullable=True)
    )
    op.add_column(
        "deployments", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Legacy active deployments keep expires_at NULL → grandfathered, never
    # auto-expired (owner can tear down manually; recorded in §13).


def downgrade() -> None:
    op.drop_column("deployments", "superseded_at")
    op.drop_column("deployments", "health_path")
    op.drop_column("deployments", "last_health_at")
    op.drop_column("deployments", "health")
