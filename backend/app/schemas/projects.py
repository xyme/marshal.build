"""Project + deployment API schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    template_id: uuid.UUID | None = None


class ProjectImportIn(BaseModel):
    """External spec-set import: pasted docs XOR a base64 zip XOR a public
    URL XOR an uploaded document (I1/I2 + I3 v1 + pdf-docx-ingestion)."""

    name: str = Field(min_length=1, max_length=120)
    source: str | None = Field(default=None, max_length=120)
    docs: dict[str, str] | None = None
    # ~1.4MB of zip fits in <2MB of base64 — inside the 2MiB body backstop.
    archive_b64: str | None = Field(default=None, max_length=2_000_000)
    # I3 v1: https-only public source (raw markdown or a marshal export zip)
    url: str | None = Field(default=None, max_length=2048)
    # PDF/DOCX upload: 2,000,000 base64 chars = exactly 1.5MB decoded — the
    # same ceiling as the URL fetch, inside the 2MiB body backstop.
    document_b64: str | None = Field(default=None, max_length=2_000_000)
    document_name: str | None = Field(default=None, max_length=255)  # audit/errors only


class DeploymentOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    build_id: uuid.UUID | None = None  # codegen provenance (S8 R5.3)
    stack_name: str | None
    app_url: str | None
    error: str | None
    timeline: list
    resources: list
    lease_account_id: str | None = None
    lease_external_id: str | None = None
    # B20 R1.4: full_governance (Enclave) | testbed — drives account naming
    mode: str = "full_governance"
    # ---- S11 deployment maturity ----
    health: str = "unknown"
    last_health_at: datetime | None = None
    expires_at: datetime | None = None
    superseded_at: datetime | None = None
    lease_state: dict | None = None  # {budget_used_usd, budget_cap_usd, expires_at}
    created_by_name: str | None = None  # history attribution
    deployed_at: datetime | None
    torn_down_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtendIn(BaseModel):
    hours: int = Field(ge=1, le=168)


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: str
    origin: str = "scratch"
    template_id: uuid.UUID | None = None
    template_name: str | None = None
    template_deprecated: bool = False
    forked_from_sample_id: uuid.UUID | None = None
    forked_from_title: str | None = None
    spec_version_count: int = 0
    last_activity_at: datetime | None = None
    # Composable agents R1.2: declared dependency graph, enriched with live
    # per-dependency deployment status at read time.
    composition: dict | None = None
    # Populated by S4 (cost-risk-governance); nullable now so the list API
    # shape is stable across sprints (project-admin-dashboard spec R1.2).
    risk_level: str | None = None
    cost_mtd_usd: float | None = None
    budget_override_usd: float | None = None  # S5 per-project budget
    my_role: str | None = None  # owner|editor|viewer (collaboration spec R2.3)
    team_id: uuid.UUID | None = None  # S15-02: null = personal project
    team_name: str | None = None
    created_at: datetime
    updated_at: datetime
    deployment: DeploymentOut | None = None

    model_config = {"from_attributes": True}


class MemberAdd(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(pattern="^(viewer|editor)$")


class MemberPatch(BaseModel):
    role: str = Field(pattern="^(viewer|editor)$")


class TransferIn(BaseModel):
    user_id: uuid.UUID


class CommentCreate(BaseModel):
    doc_type: str = Field(pattern="^(requirements|design|tasks)$")
    anchor: str = Field(min_length=1, max_length=256)
    anchor_text: str = Field(default="", max_length=512)
    body: str = Field(min_length=1, max_length=4000)


class CommentReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class PresenceIn(BaseModel):
    surface: str = Field(default="project", max_length=32)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class ProjectListOut(BaseModel):
    items: list[ProjectOut]
    total: int
    archived_count: int
    page: int
    page_size: int


class ActivityItemOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    category: str
    action: str
    actor_email: str | None = None
    detail: dict = {}


class ActivityListOut(BaseModel):
    items: list[ActivityItemOut]
    total: int
    page: int
    page_size: int
