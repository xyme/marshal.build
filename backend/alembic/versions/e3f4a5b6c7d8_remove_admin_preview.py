"""Withdraw the 'admin' demo experience value (synthetic preview removed).

The synthetic admin preview surface was withdrawn on 4 Sep 2026 (owner
decision). `experience_view` remains a presentation-only navigation preview
limited to 'business'/'power'; any persisted 'admin' selection is cleared so
API responses stay within the narrowed contract.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET experience_view = NULL WHERE experience_view = 'admin'")
    op.drop_constraint("ck_users_experience_view", "users", type_="check")
    op.create_check_constraint(
        "ck_users_experience_view",
        "users",
        "experience_view IS NULL OR experience_view IN ('business', 'power')",
    )


def downgrade() -> None:
    # Restores the wider constraint only; cleared 'admin' selections are not
    # reinstated (the preview surface no longer exists to consume them).
    op.drop_constraint("ck_users_experience_view", "users", type_="check")
    op.create_check_constraint(
        "ck_users_experience_view",
        "users",
        "experience_view IS NULL OR experience_view IN ('business', 'power', 'admin')",
    )
