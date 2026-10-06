"""Sprint 17/18: custom model endpoints + feature flags + usage estimation flag.

platform_settings.custom_model_endpoints (S17-02): OpenAI-compatible endpoint
registry — API keys live in Secrets Manager, never in this column.
platform_settings.feature_flags (S18-01): dark-launch switches (studio_enabled).
model_invocations.usage_estimated (S17-04): marks rows whose token counts were
estimated because the external endpoint omitted usage.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "custom_model_endpoints",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "feature_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "model_invocations",
        sa.Column(
            "usage_estimated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_invocations", "usage_estimated")
    op.drop_column("platform_settings", "feature_flags")
    op.drop_column("platform_settings", "custom_model_endpoints")
