"""B9: service accounts — users.kind + API tokens (integration-wave spec R2).

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-24
"""

import sqlalchemy as sa

from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # human | service — service accounts ARE user rows so collaboration,
    # audit, caps and tenancy work unchanged (FSD B9 stance).
    op.add_column(
        "users",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="human"),
    )
    op.create_table(
        "service_account_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        # SHA-256 hex of the token value; the value itself exists exactly once,
        # in the mint response.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=12), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_service_account_token_hash", "service_account_tokens", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_service_account_tokens_user", "service_account_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_service_account_tokens_user", table_name="service_account_tokens")
    op.drop_index("uq_service_account_token_hash", table_name="service_account_tokens")
    op.drop_table("service_account_tokens")
    op.drop_column("users", "kind")
