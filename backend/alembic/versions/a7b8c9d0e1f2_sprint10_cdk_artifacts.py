"""sprint10: artifact profiles + S3 artifact offload

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "codegen_builds",
        sa.Column(
            "artifact_profile", sa.String(length=16), nullable=False,
            server_default="inline-cfn",
        ),
    )
    op.add_column(
        "codegen_artifacts", sa.Column("s3_key", sa.String(length=512), nullable=True)
    )
    op.alter_column("codegen_artifacts", "content", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.alter_column("codegen_artifacts", "content", existing_type=sa.Text(), nullable=False)
    op.drop_column("codegen_artifacts", "s3_key")
    op.drop_column("codegen_builds", "artifact_profile")
