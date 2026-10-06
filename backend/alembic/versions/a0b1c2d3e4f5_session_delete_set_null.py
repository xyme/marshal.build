"""Chat session delete must not 500 (Build Log 13.5AB).

`specs.session_id` and `spec_generations.session_id` were bare FKs (S1/S2),
so deleting any session that ever produced a spec version or ran a
generation raised ForeignKeyViolation — and the route had already wiped the
DynamoDB transcript, leaving a headless "zombie" session. Both become
ON DELETE SET NULL: provenance and generation history survive on project_id.
`spec_generations.session_id` also becomes nullable.

Constraint names are resolved at runtime (the originals were created
unnamed; Postgres defaulted them to <table>_session_id_fkey) so this
migration cannot drift from whatever the live schema actually calls them.

Revision ID: a0b1c2d3e4f5
Revises: e9f0a1b2c3d4
Create Date: 2026-09-30
"""

import sqlalchemy as sa

from alembic import op

revision: str = "a0b1c2d3e4f5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None

_TABLES = ("specs", "spec_generations")


def _session_fk_name(inspector, table: str) -> str | None:
    for fk in inspector.get_foreign_keys(table):
        if fk.get("referred_table") == "chat_sessions" and fk.get("constrained_columns") == [
            "session_id"
        ]:
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        name = _session_fk_name(inspector, table)
        if name:
            op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            f"{table}_session_id_fkey",
            table,
            "chat_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.alter_column("spec_generations", "session_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Rows already detached from their session cannot satisfy NOT NULL again.
    op.execute("DELETE FROM spec_generations WHERE session_id IS NULL")
    op.alter_column("spec_generations", "session_id", existing_type=sa.Uuid(), nullable=False)
    for table in _TABLES:
        name = _session_fk_name(inspector, table)
        if name:
            op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            f"{table}_session_id_fkey", table, "chat_sessions", ["session_id"], ["id"]
        )
