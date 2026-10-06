"""sprint15 fix: team_members.tenant_id (model/migration parity)

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-29

WHY A SEPARATE REVISION: f2a3b4c5d6e7 was already applied to production, so it
is immutable — the column arrives in a new revision rather than by editing
history.

HOW IT ESCAPED THE TESTS: `TeamMember` carries `TenantMixin`, so the ORM emits
`team_members.tenant_id` in every query, but the create_table in the previous
revision omitted it. The test suite builds its schema from the MODELS
(`Base.metadata.create_all`), so the column existed there and every test passed;
only Postgres — where the schema comes from the MIGRATIONS — disagreed. It
surfaced in the live team drill as
`UndefinedColumnError: column team_members.tenant_id does not exist`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_TENANT = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.add_column(
        "team_members",
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=PLATFORM_TENANT),
    )
    op.create_index("ix_team_members_tenant_id", "team_members", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_team_members_tenant_id", table_name="team_members")
    op.drop_column("team_members", "tenant_id")
