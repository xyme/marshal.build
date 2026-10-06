"""S14-02 fix (FSD §13.5M): users.mfa_enrolled_at — temporal MFA seam.

Cognito access tokens carry no amr claim, so the admin-MFA gate compares the
session's auth_time against the platform-recorded TOTP confirmation instant.
Backfills existing enrollments from their successful `mfa_enabled` audit rows
(most recent one, so re-enrollments resolve to the strictest timestamp).

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-09-07
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("mfa_enrolled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: users who confirmed TOTP before this column existed get their
    # most recent SUCCESSFUL confirm timestamp from the audit trail. The
    # status filter keeps failed attempts (4xx/5xx rows) from loosening the
    # gate with an earlier instant.
    op.execute(
        """
        UPDATE users SET mfa_enrolled_at = sub.last_enabled
        FROM (
            SELECT actor_id, MAX(created_at) AS last_enabled
            FROM audit_logs
            WHERE action = 'mfa_enabled'
              AND actor_id IS NOT NULL
              AND (http_status IS NULL OR http_status < 400)
            GROUP BY actor_id
        ) AS sub
        WHERE users.id = sub.actor_id
        """
    )


def downgrade() -> None:
    op.drop_column("users", "mfa_enrolled_at")
