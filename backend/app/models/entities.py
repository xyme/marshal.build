"""SQLAlchemy models — schema per foundation spec design.md."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, Uuid


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # pragma: no cover - test shim
    return compiler.visit_JSON(JSON(), **kw)


def utcnow() -> datetime:
    return datetime.now(UTC)


# S12 tenancy groundwork (OQ-6): one platform tenant at Beta; every
# tenant-scoped table carries the column so GA multi-tenancy is activation,
# not schema surgery. Filtering applies at the query seams (core/tenant.py).
PLATFORM_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Base(DeclarativeBase):
    pass


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=PLATFORM_TENANT_ID, index=True
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(Base, TimestampMixin, TenantMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "account_class IN ('standard', 'demo')",
            name="ck_users_account_class",
        ),
        CheckConstraint(
            "experience_view IS NULL OR experience_view IN ('business', 'power')",
            name="ck_users_experience_view",
        ),
        CheckConstraint(
            "(account_class = 'standard' AND experience_view IS NULL) "
            "OR (account_class = 'demo' AND kind = 'human')",
            name="ck_users_demo_experience_scope",
        ),
        CheckConstraint(
            "(NOT admin_readonly) OR (kind = 'human')",
            name="ck_users_admin_readonly_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cognito_sub: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    persona: Mapped[str | None] = mapped_column(String(16), nullable=True)  # business|power
    role: Mapped[str] = mapped_column(String(16), default="business")  # business|power|admin
    # B9: human | service — service accounts are user rows (integration-wave R2.4)
    kind: Mapped[str] = mapped_column(String(16), default="human", server_default="human")
    # Presentation-only account classification. These fields never participate
    # in authorization, persona approval, or Cognito group synchronization.
    account_class: Mapped[str] = mapped_column(
        String(16), default="standard", server_default="standard"
    )
    experience_view: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # View-only admin visibility for named beta viewers (owner decision,
    # 4 Sep 2026). GET/HEAD on admin surfaces only — never grants mutations,
    # never syncs to Cognito, never alters role/role_source/persona, and is
    # held to the same admin-MFA fail-closed bar as a real administrator.
    admin_readonly: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # S14-02 fix (FSD §13.5M): MFA-detection seam. Cognito access tokens carry
    # no amr claim, so second-factor detection is temporal — a session whose
    # auth_time is at/after this platform-recorded TOTP confirmation
    # necessarily passed the Cognito challenge (an enrolled user's preferred
    # factor is always challenged at sign-in). Set by /users/me/mfa/confirm,
    # cleared by DELETE /users/me/mfa.
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 'sso' → role follows token claims; 'admin' → an admin pinned it (S3-08:
    # effective next API call, not next token refresh)
    role_source: Mapped[str] = mapped_column(String(8), default="sso")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|suspended
    budget_override_usd: Mapped[float | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )  # stored now, enforced with S5 cost caps
    use_case: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    tour_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    persona_upgrade_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # Sparse overrides over code defaults (FSD §4.4.6); adding events needs no migration
    notification_prefs: Mapped[dict] = mapped_column(JSONB, default=dict)

    @property
    def can_demo_switch(self) -> bool:
        """Whether this human may change the non-authorizing presentation preview."""
        return self.kind == "human" and self.account_class == "demo"

    @property
    def allowed_demo_experiences(self) -> list[str]:
        """Server-derived preview choices; an empty list is the default-deny contract."""
        return ["business", "power"] if self.can_demo_switch else []


class Project(Base, TimestampMixin, TenantMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # draft|spec_complete|building|deployed|inactive|archived|deleted (FSD §4.7.2)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("templates.id"), nullable=True, index=True
    )
    origin: Mapped[str] = mapped_column(String(24), default="scratch")  # scratch|template|marketplace_fork|imported
    forked_from_sample_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("marketplace_samples.id"), nullable=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-project monthly budget (S5); null inherits platform default_project_cap_usd
    budget_override_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Composable agents R1 (composable-agents spec): declared dependency graph
    # from the requirements document — {"dependencies": [{"slug", "project_id",
    # "name"}], "orchestration": "pipeline"|None, "depth": int}. Stored with
    # RESOLVED project ids (slugs embed the name and change on rename); synced
    # at every requirements write seam; None when nothing is declared.
    composition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # S15-02: optional team workspace. NULL = personal project (all pre-S15
    # projects), which keeps activation invisible to existing work.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )


class Team(Base, TimestampMixin, TenantMixin):
    """Team workspace inside one org (S15-02, owner decision D12).

    Distinct from `tenant_id`, which stays the ORG boundary reserved for GA
    multi-tenancy: teams partition work WITHIN a tenant, while org-wide
    surfaces (marketplace, templates) stay shared across teams.
    """

    __tablename__ = "teams"
    __table_args__ = (
        Index("uq_teams_tenant_name", "tenant_id", "name", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|archived
    # Per-team monthly budget for the cost dashboard (5 Sep 2026): visibility
    # and threshold coloring only — NOT enforced at the model seam (user/
    # project/platform caps stay the enforcement instruments).
    budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class TeamMember(Base, TenantMixin):
    """Team membership. `lead` manages membership and edits the team's projects;
    `member` reads them (S15-02)."""

    __tablename__ = "team_members"
    __table_args__ = (
        Index("uq_team_members_team_user", "team_id", "user_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")  # lead|member
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectMember(Base, TimestampMixin, TenantMixin):
    """Project membership (collaboration spec R1) — owner stays projects.user_id."""

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # viewer|editor (code-enforced)
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class SpecComment(Base, TenantMixin):
    """Heading-anchored spec comment; 1-level threads (collaboration spec R4).

    Anchors are heading slugs captured at creation; the backend never validates
    them against current content — orphan detection is a frontend concern.
    """

    __tablename__ = "spec_comments"
    __table_args__ = (Index("ix_spec_comments_project_doc", "project_id", "doc_type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(16))  # requirements|design|tasks
    anchor: Mapped[str] = mapped_column(String(256))
    anchor_text: Mapped[str] = mapped_column(String(512), default="")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spec_comments.id", ondelete="CASCADE"), nullable=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectPresence(Base):
    """Heartbeat presence rows (collaboration spec R6) — ephemeral telemetry."""

    __tablename__ = "project_presence"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    surface: Mapped[str] = mapped_column(String(32), default="project")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base, TimestampMixin, TenantMixin):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(120), default="New session")
    status: Mapped[str] = mapped_column(String(16), default="chatting")  # FSD §4.1.5 subset
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("templates.id"), nullable=True, index=True
    )
    mode: Mapped[str] = mapped_column(String(12), default="freeform")  # freeform|guided (§4.1.1)
    guided_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # guided-mode spec R1
    # S18 studio rail: requested overrides {model_id?, temperature?, max_tokens?}.
    # Requested ≠ effective — clamps stay in guardrails.resolve_session_call.
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Brownfield substrate (brownfield-substrate spec R1): current-state truth
    # of an EXISTING agent, attached by the session owner. Deliberately a
    # session-row column, NOT a DynamoDB message — prompt-time injection makes
    # it immune to context-window trimming and the 400KB item ceiling.
    # ≤200KB enforced at the API; 16k-char clip applies at render time.
    substrate: Mapped[str | None] = mapped_column(Text, nullable=True)
    substrate_source: Mapped[str | None] = mapped_column(String(120), nullable=True)

    @property
    def has_substrate(self) -> bool:
        return bool(self.substrate)

    @property
    def substrate_size(self) -> int:
        return len(self.substrate) if self.substrate else 0


class Spec(Base, TenantMixin):
    __tablename__ = "specs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    # Provenance only: which session generated this version. SET NULL (13.5AB)
    # — deleting a session must not 500 nor erase the project's spec history.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16))  # requirements|design|tasks
    content: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default="generated")  # generated|edited|rollback
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpecDraft(Base, TimestampMixin, TenantMixin):
    """Autosave target — one draft per user/project/doc-type; no version spam."""

    __tablename__ = "spec_drafts"
    __table_args__ = (
        UniqueConstraint("project_id", "type", "user_id", name="uq_spec_draft"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)


class SpecGeneration(Base, TenantMixin):
    """One full-set generation job per trigger (multi-doc-spec-gen spec R2)."""

    __tablename__ = "spec_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Nullable + SET NULL (13.5AB): generation history survives on project_id
    # (audit/cost attribution) after its session is deleted.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed
    docs: Mapped[list] = mapped_column(JSONB, default=list)  # [{type,status,spec_id?,version?,error?}]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Template(Base, TimestampMixin, TenantMixin):
    """Governance template (FSD §4.2.1 subset — model guardrails enforced in S2)."""

    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="custom")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|active|deprecated
    guardrails: Mapped[dict] = mapped_column(JSONB, default=dict)
    scaffolding: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)


class MarketplaceSample(Base, TimestampMixin, TenantMixin):
    """Curated sample app (FSD §4.3.5 subset — admin-curated at Alpha)."""

    __tablename__ = "marketplace_samples"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(500))
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)  # markdown ≤5000
    category: Mapped[str] = mapped_column(String(32))
    complexity: Mapped[str] = mapped_column(String(16), default="beginner")
    models_used: Mapped[list] = mapped_column(JSONB, default=list)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("templates.id"), nullable=True
    )
    spec_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # {requirements_md, design_md, tasks_md}
    assets: Mapped[dict] = mapped_column(JSONB, default=dict)  # {screenshots[], demo_url, sample_data_s3_key}
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    metadata_extra: Mapped[dict] = mapped_column(JSONB, default=dict)  # {est_build_usd, est_run_usd_month, tech_stack[]}
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # draft|published|archived + submitted|rejected|withdrawn (submissions spec).
    # S3 reserved 'approved' as a resting state; approval is instead the
    # submitted→draft TRANSITION (one curation path — deviation noted in spec).
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fork_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    # ---- S7 submissions (marketplace-submissions spec) ----
    source_project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    resubmission_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("marketplace_samples.id"), nullable=True
    )

    # One OPEN submission per project (partial unique; sqlite_where for test parity)
    __table_args__ = (
        Index(
            "uq_marketplace_open_submission",
            "source_project_id",
            unique=True,
            postgresql_where=text("status = 'submitted'"),
            sqlite_where=text("status = 'submitted'"),
        ),
    )


class AuditLog(Base, TenantMixin):
    """Action audit trail (FSD §4.6.1 — Postgres at Alpha, pipeline at Beta)."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(16), index=True)  # user|admin|deployment|security
    action: Mapped[str] = mapped_column(String(48))
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_ip: Mapped[str | None] = mapped_column(String(48), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # S14-06: set once the row is in the S3 archive; retention never deletes
    # evidence that has not been archived.
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ModelInvocation(Base, TenantMixin):
    """Every Bedrock call, platform-wide (FSD §4.6.1 model interactions)."""

    __tablename__ = "model_invocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    purpose: Mapped[str] = mapped_column(String(24))  # chat|requirements|design|tasks|title|classification|risk|codegen
    # 'platform' = in-process bedrock seam; 'runner' = reconciled from an external
    # codegen engine's reported usage (external-codegen spec R6.2)
    source: Mapped[str] = mapped_column(String(16), default="platform")
    model_id: Mapped[str] = mapped_column(String(120), index=True)
    prompt_text: Mapped[str] = mapped_column(Text)
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    response_text: Mapped[str] = mapped_column(Text, default="")
    response_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # S17: true when the endpoint omitted usage and tokens were estimated from
    # character counts (external endpoints only; Bedrock always reports usage).
    # Estimated usage is still PRICED usage — this flag keeps the row honest.
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class AiRiskAssessment(Base, TenantMixin):
    """Automated risk assessment per spec-content hash (FSD §4.6.3, S4-02/03).

    Uniqueness on (project, content_hash, rubric_version) IS the determinism
    guarantee: identical content is scored once and reread forever (AC-3).
    S6 adds workflow fields: routing, escalation, resubmission, owner comment.
    """

    __tablename__ = "ai_risk_assessments"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "content_hash", "rubric_version", "policy_version",
            name="uq_risk_project_hash_rubric_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    rubric_version: Mapped[int] = mapped_column(Integer, default=1)
    # B17: the admin risk POLICY version this row was scored under — part of
    # the determinism key; a policy change re-scores on next demand.
    policy_version: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100; null on error
    level: Mapped[str | None] = mapped_column(String(8), nullable=True)  # low|medium|high
    factors: Mapped[dict] = mapped_column(JSONB, default=dict)  # {name: {score, rationale, weight}}
    status: Mapped[str] = mapped_column(String(8), default="scored")  # scored|error
    # auto_approved|pending|approved|rejected|changes_requested (code-enforced enum)
    decision: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # ---- S6 review workflow (risk-review-workflow spec) ----
    assigned_group: Mapped[str | None] = mapped_column(String(20), nullable=True)  # managers|governance_board|admins
    routed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_to: Mapped[str | None] = mapped_column(String(20), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admin_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resubmission_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_risk_assessments.id"), nullable=True
    )
    owner_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookEndpoint(Base, TenantMixin):
    """B10 outbound webhook registration (integration-wave R3.1). The HMAC
    secret lives in Secrets Manager (marshal/webhooks/<id>) — never here."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_types: Mapped[list] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookDelivery(Base, TenantMixin):
    """B10 delivery attempt log (integration-wave R3.3): at-least-once with
    bounded retries; DEAD rows are the dead-letter view."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
        Index("ix_webhook_deliveries_sweep", "status", "next_attempt_at"),
        Index(
            "uq_webhook_delivery_dedupe",
            "endpoint_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
            sqlite_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending|delivered|failed|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceAccountToken(Base, TenantMixin):
    """B9 API token (integration-wave R2.1): SHA-256 at rest, value shown once
    at mint; mandatory expiry; instant revocation; ≤2 live per account."""

    __tablename__ = "service_account_tokens"
    __table_args__ = (
        Index("uq_service_account_token_hash", "token_hash", unique=True),
        Index("ix_service_account_tokens_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64))
    token_prefix: Mapped[str] = mapped_column(String(12))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewerGroupMember(Base, TenantMixin):
    """Reviewer group membership (risk-review-workflow spec R1).

    FSD §4.6.3's "manager" / "AI Governance Board" chains as admin-curated
    lists — the Alpha-honest org model.
    """

    __tablename__ = "reviewer_group_members"
    __table_args__ = (
        UniqueConstraint("group_name", "user_id", name="uq_reviewer_group_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_name: Mapped[str] = mapped_column(String(20), index=True)  # managers|governance_board
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformSettings(Base):
    """Singleton row (id=1): global model controls (FSD §4.6.6, S4-04).

    rate_limits and cost caps are STORED here but enforced from S5; the
    allowlist and parameter bounds enforce immediately via resolve_models
    and the bedrock seam.
    """

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    model_allowlist: Mapped[list] = mapped_column(JSONB, default=list)
    param_bounds: Mapped[dict] = mapped_column(JSONB, default=dict)
    rate_limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    cost: Mapped[dict] = mapped_column(JSONB, default=dict)
    # S9: {"provider": "internal"|"runner"} — runtime-switchable codegen engine
    # (legacy rows may still hold "kiro"; read paths normalize it to "runner")
    codegen: Mapped[dict] = mapped_column(JSONB, default=dict)
    # S12: Enclave usage bounds (platform-scale-policies R1)
    deployment_policies: Mapped[dict] = mapped_column(JSONB, default=dict)
    # S14: security controls — {"admin_mfa_required": bool, "pii_redaction": bool}
    security: Mapped[dict] = mapped_column(JSONB, default=dict)
    # S17: OpenAI-compatible endpoints beyond Bedrock (custom-model-endpoints
    # spec R2). Each: {slug, label, base_url, model_name, tier, usd_per_1k_input,
    # usd_per_1k_output, max_context_tokens, timeout_s, has_api_key, enabled}.
    # API keys live in Secrets Manager (marshal/models/<slug>), NEVER here.
    custom_model_endpoints: Mapped[list] = mapped_column(JSONB, default=list)
    # C0 connector registry (external-import-connectors spec): typed external
    # references — {slug, name, type, base_url, description, auth_header,
    # has_credential, active, last_probe, created_at, updated_at}. Credentials
    # live in Secrets Manager (marshal/connectors/<slug>), NEVER here. Not yet
    # consumed by generated agents (that is C1, decision-gated).
    connectors: Mapped[list] = mapped_column(JSONB, default=list)
    # S18: dark-launch switches — {"studio_enabled": bool}
    feature_flags: Mapped[dict] = mapped_column(JSONB, default=dict)
    # B11: org-level chat-ops — {"enabled": bool, "provider": "slack"|"teams",
    # "events": [...]}; the webhook URL is a secret (marshal/chatops/url)
    chat_ops: Mapped[dict] = mapped_column(JSONB, default=dict)
    # B17: admin-tuned risk policy — {policy_version, auto_approve_low,
    # band_low_max, band_medium_max, weights{5}, anchors{<=3}}. Empty blob =
    # version 1 = the platform defaults in services/risk.py.
    governance: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Notification(Base, TenantMixin):
    """Per-recipient notification (notifications spec R1; FSD §4.4.6)."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "uq_notifications_user_dedupe",
            "user_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
            sqlite_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    email_status: Mapped[str] = mapped_column(String(8), default="skipped")  # skipped|queued|sent|failed
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Alert(Base, TenantMixin):
    """Governance alert (cost thresholds, escalations; FSD §4.6.5/§4.6.8)."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index(
            "uq_alerts_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
            sqlite_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(24))  # cost_user|cost_project|cost_platform|rate_limit|sandbox|risk_config|risk_overdue
    severity: Mapped[str] = mapped_column(String(8), default="info")  # info|warning|critical
    scope_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    scope_project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    threshold_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    month: Mapped[str | None] = mapped_column(String(7), nullable=True)  # YYYY-MM window
    # Computed identity for dedupe (NULL scope columns defeat composite unique indexes)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)  # active|acknowledged
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class SandboxSpend(Base):
    """Daily per-lease sandbox spend from Cost Explorer (S5 R6; owner-gated)."""

    __tablename__ = "sandbox_spend"
    __table_args__ = (UniqueConstraint("lease_id", "date", name="uq_sandbox_spend_lease_date"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    lease_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leases.id"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    usd: Mapped[float] = mapped_column(Numeric(10, 4))
    source: Mapped[str] = mapped_column(String(16), default="cost_explorer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodegenBuild(Base, TenantMixin):
    """Spec → deployable-artifact build job (codegen-handoff spec R2).

    Single ACTIVE build per project via partial unique index; determinism is
    NOT promised — content_hash makes rebuild variance visible instead.
    """

    __tablename__ = "codegen_builds"
    __table_args__ = (
        Index(
            "uq_codegen_active_build",
            "project_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued','dispatched','generating','validating')"
            ),
            sqlite_where=text(
                "status IN ('queued','dispatched','generating','validating')"
            ),
        ),
        Index("ix_codegen_builds_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued|dispatched|generating|validating|ready|failed|cancelled (code-enforced;
    # 'dispatched' is external-provider only — S9)
    # internal|runner (historical rows may hold the legacy "kiro" identifier)
    provider: Mapped[str] = mapped_column(String(16), default="internal")
    phase_detail: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # ---- S9 external builds (external-codegen spec R4) ----
    external_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retried: Mapped[bool] = mapped_column(Boolean, default=False)
    # ---- S10 artifact profiles (cdk-artifacts spec R1) ----
    artifact_profile: Mapped[str] = mapped_column(String(16), default="inline-cfn")  # inline-cfn|cdk-app
    spec_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # {type: {version, content}}
    spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {code, message, findings[]}
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CodegenArtifact(Base):
    """One generated file of a build (codegen-handoff spec R2.4)."""

    __tablename__ = "codegen_artifacts"
    __table_args__ = (UniqueConstraint("build_id", "path", name="uq_codegen_artifact_path"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    build_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("codegen_builds.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(512))
    # S10: cdk-app artifact content lives in S3 (s3_key set, content NULL);
    # inline-cfn stays DB-resident. Single source of truth either way.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    language: Mapped[str | None] = mapped_column(String(24), nullable=True)


class Tenant(Base):
    """Tenancy groundwork (S12, OQ-6): a default tenant at Beta; GA activates
    real multi-tenancy without schema surgery."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lease(Base, TenantMixin):
    __tablename__ = "leases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # S12: recorded per-deployment budget (policy); passed to the provider
    # where lease templates support it (OQ-7 ADR tracks the ISB gap)
    budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    provider: Mapped[str] = mapped_column(String(16))  # direct|isb
    external_lease_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    aws_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="requested")
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Deployment(Base, TimestampMixin, TenantMixin):
    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leases.id"), nullable=True)
    # Deploy a codegen build instead of the canned sample app (S8 R5)
    build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("codegen_builds.id"), nullable=True
    )
    # B20 R1.1: custody posture — full_governance (Enclave) | testbed.
    # IMMUTABLE post-create; in-place updates inherit the active row's mode.
    mode: Mapped[str] = mapped_column(String(16), default="full_governance")
    stack_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stack_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # pending|pre_flight|leasing|deploying|updating|active|failed|superseded|
    # tearing_down|torn_down (FSD §4.5.2 + S11 in-place updates)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # ---- S11 deployment maturity ----
    health: Mapped[str] = mapped_column(String(12), default="unknown")  # unknown|healthy|degraded
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_path: Mapped[str | None] = mapped_column(String(256), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timeline: Mapped[list] = mapped_column(JSONB, default=list)
    resources: Mapped[list] = mapped_column(JSONB, default=list)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    torn_down_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageEvent(Base, TenantMixin):
    """In-house usage analytics (S16-04, owner decision D6: no third party).

    One row per funnel action, written server-side at the six instrumented
    seams (sign-in, chat, spec save, build, deploy, teardown) — there is no
    client beacon and no request-level tracking, which is the privacy story
    the docs state. `dedupe_key` collapses noisy repeats (sign_in is one row
    per user per UTC day). Analytics failures never break the business call.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_event_created", "event", "created_at"),
        Index("ix_usage_events_user_created", "user_id", "created_at"),
        Index("uq_usage_events_dedupe", "dedupe_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Denormalized from the project at write time: rollups must reflect the
    # team the work happened IN, not where the project was moved later.
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
