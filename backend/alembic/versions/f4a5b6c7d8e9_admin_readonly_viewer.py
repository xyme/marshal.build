"""Add the admin_readonly viewer capability to users.

Product decision (4 Sep 2026): designated view-only accounts may inspect
every admin surface read-only. The flag is administrator-managed, database
only (no Cognito mirror), human-only by constraint, and enforcement in
core/auth.require_role limits it to GET/HEAD under the admin-MFA bar.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "admin_readonly",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_users_admin_readonly_scope",
        "users",
        "(NOT admin_readonly) OR (kind = 'human')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_admin_readonly_scope", "users", type_="check")
    op.drop_column("users", "admin_readonly")
