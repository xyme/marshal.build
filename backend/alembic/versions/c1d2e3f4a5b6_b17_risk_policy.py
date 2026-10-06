"""B17: configurable risk rubrics — governance policy blob + versioned
assessments.

platform_settings.governance holds the admin-tuned risk policy
(auto-approve, band thresholds, factor weights, anchor overrides) plus a
server-managed policy_version. ai_risk_assessments records the version each
row was scored under, and the determinism key widens to include it: same
content + same policy = scored once; a policy change is a cache miss.
Existing rows backfill to version 1 — correct by construction, they were
scored under the platform defaults.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "governance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "ai_risk_assessments",
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint(
        "uq_risk_project_hash_rubric", "ai_risk_assessments", type_="unique"
    )
    op.create_unique_constraint(
        "uq_risk_project_hash_rubric_policy",
        "ai_risk_assessments",
        ["project_id", "content_hash", "rubric_version", "policy_version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_risk_project_hash_rubric_policy", "ai_risk_assessments", type_="unique"
    )
    op.create_unique_constraint(
        "uq_risk_project_hash_rubric",
        "ai_risk_assessments",
        ["project_id", "content_hash", "rubric_version"],
    )
    op.drop_column("ai_risk_assessments", "policy_version")
    op.drop_column("platform_settings", "governance")
