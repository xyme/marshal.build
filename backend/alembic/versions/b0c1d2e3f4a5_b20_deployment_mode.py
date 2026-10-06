"""B20 R1: deployment modes — mode column on deployments.

full_governance (the account is an Enclave: platform custody, gate enforces)
| testbed (limited user credentials vendable, gate advisory by default).
Existing rows backfill to full_governance — exactly what they were.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column(
            "mode", sa.String(length=16), nullable=False,
            server_default="full_governance",
        ),
    )


def downgrade() -> None:
    op.drop_column("deployments", "mode")
