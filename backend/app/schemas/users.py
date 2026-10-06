"""User API schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AccountClass = Literal["standard", "demo"]
DemoExperience = Literal["business", "power"]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    persona: str | None
    role: str
    account_class: AccountClass
    experience_view: DemoExperience | None
    can_demo_switch: bool
    allowed_demo_experiences: list[DemoExperience]
    admin_readonly: bool
    use_case: str | None
    onboarding_completed: bool
    tour_completed: bool
    persona_upgrade_requested: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    persona: str | None = Field(default=None, pattern="^(business|power)$")
    use_case: str | None = Field(default=None, max_length=64)
    onboarding_completed: bool | None = None
    tour_completed: bool | None = None


class DemoExperienceUpdate(BaseModel):
    experience_view: DemoExperience


class UserUpdateResult(BaseModel):
    user: UserOut
    pending_approval: bool = False


class UserStats(BaseModel):
    projects: int
    specs: int
    sessions: int
    deployments: int
