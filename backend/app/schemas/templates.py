"""Template API schemas (admin + user-facing)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

CATEGORY_PATTERN = (
    "^(chatbot|document_processing|data_analysis|workflow_automation|content_generation|custom)$"
)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    category: str = Field(default="custom", pattern=CATEGORY_PATTERN)
    guardrails: dict | None = None
    scaffolding: dict | None = None


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, pattern=CATEGORY_PATTERN)
    guardrails: dict | None = None
    scaffolding: dict | None = None


class TemplateAdminOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    description: str | None
    category: str
    status: str
    guardrails: dict
    scaffolding: dict
    usage_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TemplateAdminDetail(TemplateAdminOut):
    active_project_count: int = 0


class TemplateUserOut(BaseModel):
    """User-facing shape: no admin metadata (FSD §4.2.7)."""

    id: uuid.UUID
    name: str
    version: int
    description: str | None
    category: str
    starter_prompts: list[str] = []
    allowed_model_labels: list[str] = []

    model_config = {"from_attributes": True}


class ModelRegistryEntry(BaseModel):
    id: str
    label: str
    tier: str
    # S17 provenance plus dynamic Bedrock availability/capability metadata.
    provider: str = "bedrock"
    provider_name: str | None = None
    source: str = "aws"
    base_model_id: str | None = None
    status: str = "ACTIVE"
    availability: str = "configured"
    selectable: bool = True
    disabled_reason: str | None = None
    converse_supported: bool = True
    supports_streaming: bool = True
    input_modalities: list[str] = []
    output_modalities: list[str] = []
    inference_type: str | None = None
    inference_profiles: list[str] = []
    routing_scope: str | None = None
    access_status: str | None = None
    pricing: dict | None = None
    catalog_source: str | None = None
    catalog_stale: bool = False
    catalog_error: str | None = None
    catalog_updated_at: str | None = None
