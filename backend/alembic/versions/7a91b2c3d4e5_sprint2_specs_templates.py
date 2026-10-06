"""sprint2: templates, spec generations/drafts, spec origin, template refs

Revision ID: 7a91b2c3d4e5
Revises: 6547fdfec821
Create Date: 2026-07-24 (handwritten — cloud RDS is private; validated via
offline SQL compile and exercised by the container entrypoint on deploy)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7a91b2c3d4e5"
down_revision: str | None = "6547fdfec821"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("guardrails", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scaffolding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "spec_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("docs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spec_generations_session_id", "spec_generations", ["session_id"])
    op.create_index("ix_spec_generations_project_id", "spec_generations", ["project_id"])

    op.create_table(
        "spec_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "type", "user_id", name="uq_spec_draft"),
    )
    op.create_index("ix_spec_drafts_project_id", "spec_drafts", ["project_id"])
    op.create_index("ix_spec_drafts_user_id", "spec_drafts", ["user_id"])

    op.add_column(
        "specs",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="generated"),
    )
    op.add_column("specs", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_specs_created_by", "specs", "users", ["created_by"], ["id"])

    op.add_column("projects", sa.Column("template_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_projects_template", "projects", "templates", ["template_id"], ["id"]
    )
    op.create_index("ix_projects_template_id", "projects", ["template_id"])

    op.add_column("chat_sessions", sa.Column("template_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_chat_sessions_template", "chat_sessions", "templates", ["template_id"], ["id"]
    )
    op.create_index("ix_chat_sessions_template_id", "chat_sessions", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_template_id", table_name="chat_sessions")
    op.drop_constraint("fk_chat_sessions_template", "chat_sessions", type_="foreignkey")
    op.drop_column("chat_sessions", "template_id")
    op.drop_index("ix_projects_template_id", table_name="projects")
    op.drop_constraint("fk_projects_template", "projects", type_="foreignkey")
    op.drop_column("projects", "template_id")
    op.drop_constraint("fk_specs_created_by", "specs", type_="foreignkey")
    op.drop_column("specs", "created_by")
    op.drop_column("specs", "origin")
    op.drop_table("spec_drafts")
    op.drop_table("spec_generations")
    op.drop_table("templates")
