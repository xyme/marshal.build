"""sprint14: security controls (MFA enforcement, PII redaction) + audit archive marker

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # S14-02/S14-04: platform security controls (admin MFA requirement, PII
    # redaction toggle). Server default keeps the singleton row valid.
    op.add_column(
        "platform_settings",
        sa.Column("security", JSONB(), nullable=False, server_default="{}"),
    )
    # S14-06: archive marker — set when an audit row has been written to the S3
    # archive, so the retention sweeper never deletes unarchived evidence.
    op.add_column(
        "audit_logs",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_logs_archived_at", "audit_logs", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_archived_at", table_name="audit_logs")
    op.drop_column("audit_logs", "archived_at")
    op.drop_column("platform_settings", "security")
