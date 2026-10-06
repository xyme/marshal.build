"""Brownfield substrate on chat sessions (brownfield-substrate spec R1).

Substrate = the user's existing agent definition (prompt, tools, config),
stored on the session row and injected at prompt-assembly time.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-07
"""

import sqlalchemy as sa

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("substrate", sa.Text(), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column("substrate_source", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "substrate_source")
    op.drop_column("chat_sessions", "substrate")
