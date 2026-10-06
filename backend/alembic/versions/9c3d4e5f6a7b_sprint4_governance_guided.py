"""sprint4: risk assessments, platform settings singleton, guided sessions

Revision ID: 9c3d4e5f6a7b
Revises: 8b2c3d4e5f6a
Create Date: 2026-07-25 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9c3d4e5f6a7b"
down_revision: str | None = "8b2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Defaults per FSD §4.6.6 example; allowlist = full platform registry so
# behavior is identical until an admin edits (cost-risk-governance spec R4.5).
DEFAULT_ALLOWLIST = (
    '["us.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-4-6", '
    '"us.anthropic.claude-haiku-4-5-20251001-v1:0", "us.anthropic.claude-opus-4-8"]'
)
DEFAULT_BOUNDS = (
    '{"temperature": {"min": 0.0, "max": 1.0}, "top_p": {"min": 0.0, "max": 1.0}, '
    '"max_tokens": 8192, "max_context": 200000}'
)
DEFAULT_RATE_LIMITS = '{"per_user_rpm": 120, "per_project_rpm": 60, "platform_rpm": 1000}'
DEFAULT_COST = (
    '{"platform_budget_usd": null, "default_user_cap_usd": 500, '
    '"default_project_cap_usd": 1000, "alert_thresholds": [50, 75, 90, 100], '
    '"at_cap": "alert"}'
)


def upgrade() -> None:
    op.create_table(
        "ai_risk_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("rubric_version", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=8), nullable=True),
        sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(length=120), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "content_hash", "rubric_version", name="uq_risk_project_hash_rubric"
        ),
    )
    op.create_index("ix_ai_risk_assessments_project_id", "ai_risk_assessments", ["project_id"])
    op.create_index("ix_ai_risk_assessments_decision", "ai_risk_assessments", ["decision"])
    op.create_index(
        "ix_ai_risk_assessments_project_created",
        "ai_risk_assessments",
        ["project_id", "created_at"],
    )

    op.create_table(
        "platform_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("model_allowlist", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("param_bounds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rate_limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO platform_settings "
        "(id, model_allowlist, param_bounds, rate_limits, cost, updated_at) VALUES "
        f"(1, '{DEFAULT_ALLOWLIST}', '{DEFAULT_BOUNDS}', '{DEFAULT_RATE_LIMITS}', "
        f"'{DEFAULT_COST}', now())"
    )

    op.add_column(
        "chat_sessions",
        sa.Column("mode", sa.String(length=12), nullable=False, server_default="freeform"),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("guided_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "guided_state")
    op.drop_column("chat_sessions", "mode")
    op.drop_table("platform_settings")
    op.drop_index("ix_ai_risk_assessments_project_created", table_name="ai_risk_assessments")
    op.drop_index("ix_ai_risk_assessments_decision", table_name="ai_risk_assessments")
    op.drop_index("ix_ai_risk_assessments_project_id", table_name="ai_risk_assessments")
    op.drop_table("ai_risk_assessments")
