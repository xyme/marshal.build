"""Spec editor API schemas (project scope)."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.chat import FullSpecResponse, SpecOut  # noqa: F401 — re-exported shape


class SpecSaveIn(BaseModel):
    content: str = Field(min_length=1, max_length=400_000)


class SpecSaveOut(BaseModel):
    spec: SpecOut
    warnings: list[str] = []


class RollbackIn(BaseModel):
    version: int = Field(ge=1)


class DraftIn(BaseModel):
    content: str = Field(max_length=400_000)


class DraftOut(BaseModel):
    content: str
    updated_at: datetime

    model_config = {"from_attributes": True}
