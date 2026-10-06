"""sprint7: marketplace submission fields on samples

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "marketplace_samples", sa.Column("source_project_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_sample_source_project",
        "marketplace_samples",
        "projects",
        ["source_project_id"],
        ["id"],
    )
    op.add_column(
        "marketplace_samples",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("marketplace_samples", sa.Column("reviewed_by", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_sample_reviewed_by", "marketplace_samples", "users", ["reviewed_by"], ["id"]
    )
    op.add_column(
        "marketplace_samples",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "marketplace_samples", sa.Column("review_feedback", sa.Text(), nullable=True)
    )
    op.add_column(
        "marketplace_samples", sa.Column("resubmission_of", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_sample_resubmission_of",
        "marketplace_samples",
        "marketplace_samples",
        ["resubmission_of"],
        ["id"],
    )
    # One OPEN submission per project (partial unique index)
    op.create_index(
        "uq_marketplace_open_submission",
        "marketplace_samples",
        ["source_project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'submitted'"),
    )


def downgrade() -> None:
    op.drop_index("uq_marketplace_open_submission", table_name="marketplace_samples")
    op.drop_constraint("fk_sample_resubmission_of", "marketplace_samples", type_="foreignkey")
    op.drop_column("marketplace_samples", "resubmission_of")
    op.drop_column("marketplace_samples", "review_feedback")
    op.drop_column("marketplace_samples", "reviewed_at")
    op.drop_constraint("fk_sample_reviewed_by", "marketplace_samples", type_="foreignkey")
    op.drop_column("marketplace_samples", "reviewed_by")
    op.drop_column("marketplace_samples", "submitted_at")
    op.drop_constraint("fk_sample_source_project", "marketplace_samples", type_="foreignkey")
    op.drop_column("marketplace_samples", "source_project_id")
