"""Admin user management API (project-admin-dashboard spec R4/R5)."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_role
from app.core.db import get_db
from app.models import User
from app.services import admin_users as svc
from app.services import audit

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
    dependencies=[Depends(require_role("admin"))],
)


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str | None
    role: str
    role_source: str
    persona: str | None
    status: str
    account_class: str
    experience_view: str | None
    admin_readonly: bool
    persona_upgrade_requested: bool
    budget_override_usd: float | None
    onboarding_completed: bool
    created_at: datetime
    project_count: int = 0
    last_active_at: datetime | None = None


class AdminUserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int
    page: int
    page_size: int


class AdminUserStatsOut(BaseModel):
    total: int
    active_30d: int
    business: int
    power: int
    admins: int


class AdminUserUpdateIn(BaseModel):
    role: str | None = Field(default=None, pattern="^(business|power|admin)$")
    persona: str | None = Field(default=None, pattern="^(business|power)$")
    status: str | None = Field(default=None, pattern="^(active|suspended)$")
    budget_override_usd: float | None = Field(default=None, ge=0, le=100_000)
    clear_budget: bool = False
    account_class: str | None = Field(default=None, pattern="^(standard|demo)$")
    experience_view: str | None = Field(
        default=None, pattern="^(business|power)$"
    )
    clear_experience_view: bool = False
    # View-only admin visibility (product decision, 4 Sep 2026). None = unchanged.
    admin_readonly: bool | None = None


class PersonaDecideIn(BaseModel):
    approve: bool


async def _target_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> User:
    target = await db.get(User, user_id)
    if target is None or target.kind != "human":
        # Human-user administration must never become a back door into the
        # separate service-account lifecycle.
        raise HTTPException(status_code=404, detail="User not found")
    return target


def _out(item: dict) -> AdminUserOut:
    out = AdminUserOut.model_validate(item["user"])
    out.project_count = item["project_count"]
    out.last_active_at = item["last_active_at"]
    return out


@router.get("", response_model=AdminUserListOut)
async def list_users(
    q: str | None = None,
    role: str | None = Query(None, pattern="^(business|power|admin)$"),
    status: str | None = Query(None, pattern="^(active|suspended)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> AdminUserListOut:
    items, total = await svc.list_users(
        db, q=q, role=role, status=status, page=page, page_size=page_size
    )
    return AdminUserListOut(
        items=[_out(i) for i in items], total=total, page=page, page_size=page_size
    )


@router.get("/stats", response_model=AdminUserStatsOut)
async def stats(db: AsyncSession = Depends(get_db)) -> AdminUserStatsOut:
    return AdminUserStatsOut(**await svc.user_stats(db))


@router.put("/{user_id}", response_model=AdminUserOut)
async def update_user(
    payload: AdminUserUpdateIn,
    request: Request,
    target: User = Depends(_target_user),
    acting: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        target, change = await svc.update_user(
            db,
            target,
            acting,
            role=payload.role,
            persona=payload.persona,
            status=payload.status,
            budget_override_usd=payload.budget_override_usd,
            clear_budget=payload.clear_budget,
            account_class=payload.account_class,
            experience_view=payload.experience_view,
            clear_experience_view=payload.clear_experience_view,
            admin_readonly=payload.admin_readonly,
        )
    except svc.AdminUserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except svc.CognitoSyncError as exc:
        raise HTTPException(
            status_code=502, detail=f"Cognito sync failed; no changes applied: {exc}"
        ) from exc
    audit.set_audit_detail(request, **change, target_email=target.email)
    return _out({"user": target, "project_count": 0, "last_active_at": None})


@router.post("/{user_id}/persona-request/decide", response_model=AdminUserOut)
async def decide_persona(
    payload: PersonaDecideIn,
    request: Request,
    target: User = Depends(_target_user),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        target, change = await svc.decide_persona_request(db, target, approve=payload.approve)
    except svc.AdminUserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, **change, target_email=target.email)
    return _out({"user": target, "project_count": 0, "last_active_at": None})


# ------------------------------------------- S15-03 lifecycle & access review


@router.post("/{user_id}/offboard")
async def offboard_user(
    request: Request,
    target: User = Depends(_target_user),
    acting: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Permanent access removal: suspend + strip team and project memberships.

    Owned projects are REPORTED, not touched — see the service docstring.
    """
    try:
        result = await svc.offboard_user(db, target, acting)
    except svc.AdminUserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except svc.CognitoSyncError as exc:
        raise HTTPException(
            status_code=502, detail=f"Cognito sync failed; no changes applied: {exc}"
        ) from exc
    audit.set_audit_detail(
        request,
        target_email=target.email,
        teams_removed=result["teams_removed"],
        project_shares_removed=result["project_shares_removed"],
        owned_projects=len(result["owned_projects_needing_transfer"]),
    )
    return result


@router.get("/access-review")
async def access_review(
    dormant_days: int = Query(svc.DORMANT_DAYS_DEFAULT, ge=7, le=365),
    fmt: str = Query("json", alias="format", pattern="^(json|csv)$"),
    _: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Periodic access review: who holds what role, in which teams, last active.

    JSON for the admin screen, CSV for the reviewer who has to sign it off.
    """
    report = await svc.access_review(db, dormant_days=dormant_days)
    if fmt == "json":
        return report

    import csv
    import io

    from fastapi.responses import StreamingResponse

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "email", "name", "platform_role", "role_source", "persona", "status",
            "teams", "owned_projects", "last_active_at", "dormant", "created_at",
        ])
        for row in report["users"]:
            writer.writerow([
                row["email"], row["name"] or "", row["platform_role"],
                row["role_source"] or "", row["persona"] or "", row["status"],
                "; ".join(row["teams"]), row["owned_projects"],
                row["last_active_at"] or "never", "yes" if row["dormant"] else "no",
                row["created_at"] or "",
            ])
        yield buf.getvalue()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="marshal-access-review-{stamp}.csv"'
        },
    )
