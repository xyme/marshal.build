"""sprint9: external codegen builds (workspace sync)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-26 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "codegen_builds", sa.Column("external_job_id", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "codegen_builds",
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "codegen_builds",
        sa.Column("retried", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The single-active partial index must cover the new 'dispatched' state
    op.drop_index("uq_codegen_active_build", table_name="codegen_builds")
    op.create_index(
        "uq_codegen_active_build",
        "codegen_builds",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued','dispatched','generating','validating')"
        ),
    )

    op.add_column(
        "model_invocations",
        sa.Column(
            "source", sa.String(length=16), nullable=False, server_default="platform"
        ),
    )

    op.add_column(
        "platform_settings",
        sa.Column("codegen", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "codegen")
    op.drop_column("model_invocations", "source")
    op.drop_index("uq_codegen_active_build", table_name="codegen_builds")
    op.create_index(
        "uq_codegen_active_build",
        "codegen_builds",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','generating','validating')"),
    )
    op.drop_column("codegen_builds", "retried")
    op.drop_column("codegen_builds", "dispatched_at")
    op.drop_column("codegen_builds", "external_job_id")
