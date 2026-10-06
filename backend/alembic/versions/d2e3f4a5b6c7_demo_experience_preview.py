"""Add non-authorizing demo experience preview metadata to users.

`account_class` is an administrator-managed local classification. The optional
`experience_view` controls presentation only; role, persona, and Cognito group
membership remain the authorization sources. Existing and newly JIT-created
users default safely to standard accounts with no preview override.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "account_class",
            sa.String(length=16),
            nullable=False,
            server_default="standard",
        ),
    )
    op.add_column(
        "users",
        sa.Column("experience_view", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_account_class",
        "users",
        "account_class IN ('standard', 'demo')",
    )
    op.create_check_constraint(
        "ck_users_experience_view",
        "users",
        "experience_view IS NULL OR experience_view IN ('business', 'power', 'admin')",
    )
    op.create_check_constraint(
        "ck_users_demo_experience_scope",
        "users",
        "(account_class = 'standard' AND experience_view IS NULL) "
        "OR (account_class = 'demo' AND kind = 'human')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_users_demo_experience_scope", "users", type_="check"
    )
    op.drop_constraint("ck_users_experience_view", "users", type_="check")
    op.drop_constraint("ck_users_account_class", "users", type_="check")
    op.drop_column("users", "experience_view")
    op.drop_column("users", "account_class")
