"""sprint12: tenancy groundwork (OQ-6) — tenants table + tenant_id everywhere

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-27 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)

Single platform tenant at Beta: every tenant-scoped table gains
tenant_id (server_default = the platform tenant) so existing rows backfill
implicitly. GA multi-tenancy is activation, not schema surgery.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_TENANT = "00000000-0000-0000-0000-000000000001"

TENANT_TABLES = [
    "users", "projects", "chat_sessions", "specs", "spec_drafts",
    "spec_generations", "templates", "marketplace_samples", "audit_logs",
    "model_invocations", "ai_risk_assessments", "reviewer_group_members",
    "notifications", "alerts", "leases", "deployments",
    "project_members", "spec_comments", "codegen_builds",
]


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        f"INSERT INTO tenants (id, name, created_at) "
        f"VALUES ('{PLATFORM_TENANT}', 'platform', NOW())"
    )
    for table in TENANT_TABLES:
        op.add_column(
            table,
            sa.Column(
                "tenant_id", sa.Uuid(), nullable=False,
                server_default=sa.text(f"'{PLATFORM_TENANT}'"),
            ),
        )
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)
        op.drop_column(table, "tenant_id")
    op.drop_table("tenants")
