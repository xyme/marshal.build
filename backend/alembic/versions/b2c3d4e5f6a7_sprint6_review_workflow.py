"""sprint6: reviewer groups + risk review workflow fields

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reviewer_group_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_name", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_name", "user_id", name="uq_reviewer_group_member"),
    )
    op.create_index(
        "ix_reviewer_group_members_group_name", "reviewer_group_members", ["group_name"]
    )
    op.create_index(
        "ix_reviewer_group_members_user_id", "reviewer_group_members", ["user_id"]
    )

    # 'changes_requested' is 17 chars — as-built VARCHAR(16) must widen (spec note)
    op.alter_column(
        "ai_risk_assessments",
        "decision",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=True,
    )
    op.add_column(
        "ai_risk_assessments", sa.Column("assigned_group", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "ai_risk_assessments",
        sa.Column("routed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_risk_assessments", sa.Column("escalated_to", sa.String(length=20), nullable=True)
    )
    op.add_column(
        "ai_risk_assessments",
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_risk_assessments",
        sa.Column("admin_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ai_risk_assessments", sa.Column("resubmission_of", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_risk_resubmission_of",
        "ai_risk_assessments",
        "ai_risk_assessments",
        ["resubmission_of"],
        ["id"],
    )
    op.add_column(
        "ai_risk_assessments", sa.Column("owner_comment", sa.Text(), nullable=True)
    )
    op.add_column(
        "ai_risk_assessments",
        sa.Column("owner_comment_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_risk_assessments", "owner_comment_at")
    op.drop_column("ai_risk_assessments", "owner_comment")
    op.drop_constraint("fk_risk_resubmission_of", "ai_risk_assessments", type_="foreignkey")
    op.drop_column("ai_risk_assessments", "resubmission_of")
    op.drop_column("ai_risk_assessments", "admin_alerted_at")
    op.drop_column("ai_risk_assessments", "escalated_at")
    op.drop_column("ai_risk_assessments", "escalated_to")
    op.drop_column("ai_risk_assessments", "routed_at")
    op.drop_column("ai_risk_assessments", "assigned_group")
    op.alter_column(
        "ai_risk_assessments",
        "decision",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
    op.drop_index("ix_reviewer_group_members_user_id", table_name="reviewer_group_members")
    op.drop_index("ix_reviewer_group_members_group_name", table_name="reviewer_group_members")
    op.drop_table("reviewer_group_members")
