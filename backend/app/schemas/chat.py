"""Chat API schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    template_id: uuid.UUID | None = None
    mode: str | None = Field(default=None, pattern="^(freeform|guided)$")
    # Start a session on an existing project (collaboration spec R3.2; editor+)
    project_id: uuid.UUID | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    model_id: str | None
    project_id: uuid.UUID | None
    template_id: uuid.UUID | None = None
    mode: str = "freeform"
    guided_state: dict | None = None
    params: dict | None = None  # requested rail overrides (S18)
    # Collaboration (spec R3.2): creator attribution for shared-project sessions
    creator_name: str | None = None
    is_mine: bool | None = None
    project_name: str | None = None
    # Brownfield substrate presence (spec R1) — never the content itself; the
    # session view doesn't need 200KB riding along on every list call.
    has_substrate: bool = False
    substrate_source: str | None = None
    substrate_size: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GuidedAnswerIn(BaseModel):
    step: str = Field(pattern="^(use_case|context|behavior)$")
    answers: dict


class GuidedClarifyIn(BaseModel):
    answers: list[dict] = []  # [{question_id, answer? , skip?}]


class SessionUpdate(BaseModel):
    """PATCH /sessions/{id}: rename and/or studio rail overrides (S18 R3.3).

    Requested values are stored as asked; clamping happens at call time in
    guardrails.resolve_session_call so enforcement and display stay one path.
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    model_id: str | None = Field(default=None, max_length=120)
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=256, le=65536)


class SubstrateIn(BaseModel):
    """PUT /sessions/{id}/substrate (brownfield-substrate spec R1).

    content XOR document_b64 (pdf-docx-ingestion R1.3): pasted/read text, or
    a single PDF/DOCX document the server extracts. 200KB stored cap on the
    TEXT either way; the prompt render applies its own 16k clip separately.
    """

    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    # 2,000,000 base64 chars = exactly 1.5MB decoded (the import ceiling).
    document_b64: str | None = Field(default=None, max_length=2_000_000)
    source: str | None = Field(default=None, max_length=120)


class PromptSegment(BaseModel):
    label: str
    content: str


class PromptContextOut(BaseModel):
    """What the platform sends with the next message (studio spec R1.1)."""

    segments: list[PromptSegment]
    model_id: str
    params: dict


class EffectiveParamsOut(BaseModel):
    requested: dict
    effective: dict
    clamped_by: dict
    allowed_models: list[str]
    template_name: str | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class MessageOut(BaseModel):
    role: str
    content: str
    model_id: str | None = None
    sk: str


class SpecOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    type: str
    content: str
    model_id: str | None
    origin: str = "generated"
    created_at: datetime

    model_config = {"from_attributes": True}


class SpecVersionInfo(BaseModel):
    id: uuid.UUID
    version: int
    origin: str = "generated"
    model_id: str | None = None
    created_by_name: str | None = None  # author attribution (collaboration R3.1)
    created_at: datetime

    model_config = {"from_attributes": True}


class DocSpecState(BaseModel):
    latest: SpecOut | None = None
    versions: list[SpecVersionInfo] = []


class FullSpecResponse(BaseModel):
    requirements: DocSpecState
    design: DocSpecState
    tasks: DocSpecState


class GenerationOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID | None  # None once the originating session is deleted (13.5AB)
    project_id: uuid.UUID | None
    status: str
    docs: list
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}
