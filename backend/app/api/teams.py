"""Team workspace endpoints (S15-02).

Two surfaces, deliberately separated:
  - `/teams` — what a member may see: their own teams and who is in them.
  - `/admin/teams` — org administration: create, rename, archive, set membership.

Project→team assignment lives on the project resource (owner-level action).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    get_current_user,
    require_admin_security_if_admin,
    require_role,
)
from app.core.db import get_db
from app.models import Team, User
from app.services import audit
from app.services import teams as teams_svc

router = APIRouter(prefix="/teams", tags=["teams"])
admin_router = APIRouter(
    prefix="/admin/teams",
    tags=["admin-teams"],
    dependencies=[Depends(require_role("admin"))],
)


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class TeamUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    # Per-team monthly budget (visibility only; not a model-seam cap).
    budget_usd: float | None = Field(default=None, ge=0, le=100_000)
    clear_budget: bool = False
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class MemberEntry(BaseModel):
    user_id: uuid.UUID
    role: str = Field(default="member", pattern="^(lead|member)$")


class MembersIn(BaseModel):
    members: list[MemberEntry]


class TeamAssignIn(BaseModel):
    team_id: uuid.UUID | None = None


class TeamOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: str
    budget_usd: float | None = None
    member_count: int
    project_count: int
    created_at: datetime
    my_role: str | None = None


# ------------------------------------------------------------- member surface


@router.get("", response_model=list[TeamOut])
async def my_teams(
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> list[TeamOut]:
    """Teams the caller belongs to (admins see every active team)."""
    memberships = await teams_svc.memberships_for_user(db, user.id)
    out: list[TeamOut] = []
    for team in await teams_svc.list_teams(db):
        role = memberships.get(team.id)
        if role is None and user.role != "admin":
            continue
        out.append(TeamOut(**await teams_svc.team_summary(db, team), my_role=role))
    return out


@router.get("/{team_id}/members")
async def team_members(
    team_id: uuid.UUID,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Membership roster. Non-members get 404 — team existence is not disclosed
    (same stance as project membership, collaboration spec R1.2)."""
    team = await db.get(Team, team_id)
    memberships = await teams_svc.memberships_for_user(db, user.id)
    if team is None or (team_id not in memberships and user.role != "admin"):
        raise HTTPException(status_code=404, detail="Team not found")
    return {
        "team": await teams_svc.team_summary(db, team),
        "members": await teams_svc.members_with_users(db, team_id),
    }


# -------------------------------------------------------------- admin surface


@admin_router.get("", response_model=list[TeamOut])
async def admin_list_teams(
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> list[TeamOut]:
    return [
        TeamOut(**await teams_svc.team_summary(db, team))
        for team in await teams_svc.list_teams(db, include_archived=include_archived)
    ]


@admin_router.post("", response_model=TeamOut, status_code=201)
async def admin_create_team(
    payload: TeamIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TeamOut:
    try:
        team = await teams_svc.create_team(
            db, name=payload.name, description=payload.description, actor=admin
        )
    except teams_svc.TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, resource_id=str(team.id), name=team.name)
    return TeamOut(**await teams_svc.team_summary(db, team))


@admin_router.put("/{team_id}", response_model=TeamOut)
async def admin_update_team(
    team_id: uuid.UUID,
    payload: TeamUpdateIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TeamOut:
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    before = {
        "name": team.name, "description": team.description, "status": team.status,
        "budget_usd": float(team.budget_usd) if team.budget_usd is not None else None,
    }
    try:
        team = await teams_svc.update_team(
            db,
            team,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            budget_usd=payload.budget_usd,
            clear_budget=payload.clear_budget,
        )
    except teams_svc.TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, resource_id=str(team.id), before=before,
        after={
            "name": team.name, "description": team.description, "status": team.status,
            "budget_usd": float(team.budget_usd) if team.budget_usd is not None else None,
        },
    )
    return TeamOut(**await teams_svc.team_summary(db, team))


@admin_router.put("/{team_id}/members")
async def admin_set_members(
    team_id: uuid.UUID,
    payload: MembersIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    try:
        members = await teams_svc.set_members(
            db,
            team,
            [{"user_id": e.user_id, "role": e.role} for e in payload.members],
            admin,
        )
    except teams_svc.TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, resource_id=str(team.id), team=team.name, member_count=len(members)
    )
    return {"team_id": str(team.id), "members": members}
