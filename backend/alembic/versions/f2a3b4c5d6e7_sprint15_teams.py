"""sprint15: team workspaces (product decision D12 — teams within one org)

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-29

DESIGN NOTE (deviation from the S15-02 acceptance text, recorded in FSD §13):
the spec said team scoping would ride the S12 `tenant_id` filters. It does not,
and should not. `tenant_id` is the ORG boundary reserved for GA multi-tenancy
(OQ-6); a team is a workspace INSIDE one org. Making teams tenants would have
fragmented org-wide surfaces — the marketplace and template catalogue are meant
to be shared across teams, and tenant filters would have hidden them.

So teams are additive: a new table plus an optional `projects.team_id`, layered
on the existing project access seam. The S12 tenant filters stay exactly as they
are, and GA multi-org activation remains a separate, untouched step.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_TENANT = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=PLATFORM_TENANT),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_teams_tenant_id", "teams", ["tenant_id"])
    # Team names are the human handle in admin UI and chargeback reports —
    # duplicates would make both ambiguous.
    op.create_index("uq_teams_tenant_name", "teams", ["tenant_id", "name"], unique=True)

    op.create_table(
        "team_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("team_id", sa.Uuid(), sa.ForeignKey("teams.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        # lead = manages membership + edits the team's projects; member = read
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column("added_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])
    op.create_index("uq_team_members_team_user", "team_members", ["team_id", "user_id"],
                    unique=True)

    # Nullable on purpose: personal projects (every project today) stay
    # team-less, so activation changes nothing for existing work.
    op.add_column("projects", sa.Column("team_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_projects_team_id", "projects", "teams", ["team_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_projects_team_id", "projects", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_team_id", table_name="projects")
    op.drop_constraint("fk_projects_team_id", "projects", type_="foreignkey")
    op.drop_column("projects", "team_id")
    op.drop_index("uq_team_members_team_user", table_name="team_members")
    op.drop_index("ix_team_members_user_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("uq_teams_tenant_name", table_name="teams")
    op.drop_index("ix_teams_tenant_id", table_name="teams")
    op.drop_table("teams")
