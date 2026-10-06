"""Sprint 18: studio build experience — per-session requested params.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Requested (pre-clamp) session overrides from the studio config rail:
    # {"model_id": str?, "temperature": float?, "max_tokens": int?}
    op.add_column("chat_sessions", sa.Column("params", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "params")
