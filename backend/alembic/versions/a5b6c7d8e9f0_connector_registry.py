"""Add the connector registry column to platform settings.

C0 of the external-import-connectors spec (5 Sep 2026): typed references to
external systems (data sources, HTTP APIs, agent registries, MCP servers)
registered and probed by admins. Credentials live in Secrets Manager
(marshal/connectors/<slug>), never in this column. Generated agents do not
consume connectors yet — that is the decision-gated C1 phase.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "connectors",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_settings", "connectors")
