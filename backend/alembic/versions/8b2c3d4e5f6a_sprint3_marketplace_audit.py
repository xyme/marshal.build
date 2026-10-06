"""sprint3: marketplace samples, audit logs, model invocations, project
lifecycle states, admin user management fields

Revision ID: 8b2c3d4e5f6a
Revises: 7a91b2c3d4e5
Create Date: 2026-07-25 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8b2c3d4e5f6a"
down_revision: str | None = "7a91b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------ marketplace
    op.create_table(
        "marketplace_samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("long_description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("complexity", sa.String(length=16), nullable=False),
        sa.Column("models_used", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=True),
        sa.Column("spec_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("assets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("keywords", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fork_count", sa.Integer(), nullable=False),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"]),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_marketplace_samples_status", "marketplace_samples", ["status"])
    op.create_index(
        "ix_marketplace_samples_status_published",
        "marketplace_samples",
        ["status", sa.literal_column("published_at DESC")],
    )

    # ------------------------------------------------------------------ audit
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_ip", sa.String(length=48), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_actor_id", "audit_logs", ["actor_id"])
    op.create_index("ix_audit_logs_category", "audit_logs", ["category"])
    op.create_index("ix_audit_logs_project_id", "audit_logs", ["project_id"])
    op.create_index(
        "ix_audit_logs_actor_created", "audit_logs", ["actor_id", "created_at"]
    )

    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("generation_id", sa.Uuid(), nullable=True),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_invocations_created_at", "model_invocations", ["created_at"])
    op.create_index("ix_model_invocations_user_id", "model_invocations", ["user_id"])
    op.create_index("ix_model_invocations_project_id", "model_invocations", ["project_id"])
    op.create_index("ix_model_invocations_model_id", "model_invocations", ["model_id"])
    op.create_index(
        "ix_model_invocations_user_created", "model_invocations", ["user_id", "created_at"]
    )

    # --------------------------------------------------------------- projects
    op.add_column(
        "projects",
        sa.Column("origin", sa.String(length=24), nullable=False, server_default="scratch"),
    )
    op.add_column("projects", sa.Column("forked_from_sample_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_projects_forked_from_sample",
        "projects",
        "marketplace_samples",
        ["forked_from_sample_id"],
        ["id"],
    )
    op.add_column(
        "projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Projects created from a template before this migration keep origin
    # consistency for the dashboard's origin chip.
    op.execute(
        "UPDATE projects SET origin = 'template' WHERE template_id IS NOT NULL"
    )

    # ------------------------------------------------------------------ users
    op.add_column(
        "users",
        sa.Column("role_source", sa.String(length=8), nullable=False, server_default="sso"),
    )
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.add_column(
        "users",
        sa.Column("budget_override_usd", sa.Numeric(precision=10, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "budget_override_usd")
    op.drop_column("users", "status")
    op.drop_column("users", "role_source")
    op.drop_column("projects", "deleted_at")
    op.drop_column("projects", "archived_at")
    op.drop_constraint("fk_projects_forked_from_sample", "projects", type_="foreignkey")
    op.drop_column("projects", "forked_from_sample_id")
    op.drop_column("projects", "origin")
    op.drop_index("ix_model_invocations_user_created", table_name="model_invocations")
    op.drop_index("ix_model_invocations_model_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_project_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_user_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_created_at", table_name="model_invocations")
    op.drop_table("model_invocations")
    op.drop_index("ix_audit_logs_actor_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_project_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_category", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_marketplace_samples_status_published", table_name="marketplace_samples")
    op.drop_index("ix_marketplace_samples_status", table_name="marketplace_samples")
    op.drop_table("marketplace_samples")
