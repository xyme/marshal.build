"""sprint8: codegen builds + artifacts + deployment provenance

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "codegen_builds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("phase_detail", sa.String(length=256), nullable=True),
        sa.Column("spec_snapshot", JSONB(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("error", JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_codegen_builds_project_id", "codegen_builds", ["project_id"])
    op.create_index(
        "ix_codegen_builds_project_created", "codegen_builds", ["project_id", "created_at"]
    )
    op.create_index(
        "uq_codegen_active_build",
        "codegen_builds",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','generating','validating')"),
    )

    op.create_table(
        "codegen_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("build_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=24), nullable=True),
        sa.ForeignKeyConstraint(["build_id"], ["codegen_builds.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("build_id", "path", name="uq_codegen_artifact_path"),
    )
    op.create_index("ix_codegen_artifacts_build_id", "codegen_artifacts", ["build_id"])

    op.add_column("deployments", sa.Column("build_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_deployment_build", "deployments", "codegen_builds", ["build_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_deployment_build", "deployments", type_="foreignkey")
    op.drop_column("deployments", "build_id")
    op.drop_index("ix_codegen_artifacts_build_id", table_name="codegen_artifacts")
    op.drop_table("codegen_artifacts")
    op.drop_index("uq_codegen_active_build", table_name="codegen_builds")
    op.drop_index("ix_codegen_builds_project_created", table_name="codegen_builds")
    op.drop_index("ix_codegen_builds_project_id", table_name="codegen_builds")
    op.drop_table("codegen_builds")
