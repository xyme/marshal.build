"""Marketplace schemas (marketplace spec R1–R6)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SampleCardOut(BaseModel):
    """Catalog card — no heavyweight fields."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    category: str
    complexity: str
    models_used: list[str] = []
    fork_count: int
    view_count: int
    published_at: datetime | None = None
    template_id: uuid.UUID | None = None
    status: str | None = None  # populated on ADMIN listings only
    # "Contributed by" credit for submission-originated samples (submissions R2.6)
    contributed_by: str | None = None


class SampleDetailOut(SampleCardOut):
    long_description: str | None = None
    spec_snapshot: dict = {}
    assets: dict = {}
    keywords: list[str] = []
    metadata_extra: dict = {}
    author_name: str | None = None
    template_name: str | None = None
    template_deprecated: bool = False


class SampleAdminOut(SampleDetailOut):
    status: str
    created_at: datetime
    updated_at: datetime


class SampleListOut(BaseModel):
    items: list[SampleCardOut]
    total: int
    page: int
    page_size: int


class CategoryCountOut(BaseModel):
    category: str
    count: int


class SampleUpsertIn(BaseModel):
    title: str = Field(min_length=3, max_length=60)
    description: str = Field(min_length=10, max_length=500)
    long_description: str | None = Field(default=None, max_length=5000)
    category: str
    complexity: str = Field(default="beginner", pattern="^(beginner|intermediate|advanced)$")
    models_used: list[str] = []
    template_id: uuid.UUID | None = None
    spec_snapshot: dict = {}
    assets: dict = {}
    keywords: list[str] = []
    metadata_extra: dict = {}


class SampleImportIn(BaseModel):
    project_id: uuid.UUID


class ForkIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)


class ForkOut(BaseModel):
    project_id: uuid.UUID
    warnings: list[str] = []


class SubmitIn(BaseModel):
    """Power-user project submission (marketplace-submissions spec R1)."""

    title: str = Field(min_length=3, max_length=60)
    summary: str = Field(min_length=10, max_length=500)
    category: str
    keywords: list[str] = []


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    description: str
    category: str
    status: str
    source_project_id: uuid.UUID | None = None
    submitted_at: datetime | None = None
    reviewed_at: datetime | None = None
    review_feedback: str | None = None
    resubmission_of: uuid.UUID | None = None
    author_name: str | None = None
    author_email: str | None = None
    project_name: str | None = None


class RejectIn(BaseModel):
    feedback: str = Field(min_length=3, max_length=2000)
