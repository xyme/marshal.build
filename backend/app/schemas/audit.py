"""Admin audit viewer schemas (audit-logging spec R3/R4)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditEntryOut(BaseModel):
    """Unified timeline row — audit_logs and model_invocations projected alike."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    source: str  # audit|model
    category: str
    action: str
    actor_id: uuid.UUID | None = None
    actor_email: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    project_id: uuid.UUID | None = None
    http_status: int | None = None
    summary: str | None = None  # model rows: "model_id · N tok · $cost"


class AuditDetailOut(AuditEntryOut):
    detail: dict = {}


class AuditListOut(BaseModel):
    items: list[AuditEntryOut]
    total: int
    page: int
    page_size: int
