"""User profile endpoints (user-persona-profile spec)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_admin_security_if_admin
from app.core.config import get_settings
from app.core.db import get_db
from app.models import ChatSession, Deployment, Project, Spec, User
from app.schemas.users import (
    DemoExperienceUpdate,
    UserOut,
    UserStats,
    UserUpdate,
    UserUpdateResult,
)
from app.services import analytics, audit

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_me(user: User = Depends(get_current_user)) -> User:
    # S16-04 funnel step 1: every page load bootstraps through /me, so this is
    # the sign-in signal — deduped to one row per user per day, no beacons.
    analytics.track_sign_in(user.id)
    return user


@router.put("/me", response_model=UserUpdateResult)
async def update_me(
    payload: UserUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserUpdateResult:
    pending_approval = False
    if payload.name is not None:
        user.name = payload.name
    if payload.use_case is not None:
        user.use_case = payload.use_case
    if payload.onboarding_completed is not None:
        user.onboarding_completed = payload.onboarding_completed
    if payload.tour_completed is not None:
        user.tour_completed = payload.tour_completed

    if payload.persona is not None:
        # Persona validation matrix (user-persona-profile design.md)
        if payload.persona == "power" and user.role == "business":
            user.persona = user.persona or "business"
            user.persona_upgrade_requested = True
            pending_approval = True
        else:
            user.persona = payload.persona
            if payload.persona == "business":
                user.persona_upgrade_requested = False

    await db.commit()
    await db.refresh(user)
    return UserUpdateResult(user=UserOut.model_validate(user), pending_approval=pending_approval)


@router.put("/me/demo-experience", response_model=UserOut)
async def update_demo_experience(
    payload: DemoExperienceUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    """Change presentation metadata only; real authorization remains untouched."""
    if not user.can_demo_switch:
        audit.set_audit_detail(
            request,
            requested_experience=payload.experience_view,
            real_role=user.role,
        )
        raise HTTPException(
            status_code=403,
            detail="Experience Preview is available only to demo human accounts.",
        )

    previous = user.experience_view
    user.experience_view = payload.experience_view
    await db.commit()
    await db.refresh(user)
    audit.set_audit_detail(
        request,
        from_experience=previous,
        to_experience=user.experience_view,
        real_role=user.role,
        real_persona=user.persona,
    )
    return UserOut.model_validate(user)


@router.put("/me/notifications")
async def update_notification_prefs(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Preference matrix per FSD §4.4.6 (notifications spec R2)."""
    from fastapi import HTTPException

    from app.services.notifications import DEFAULT_PREFS, VALID_EVENT_KEYS

    cleaned: dict = {}
    for event_key, channels in payload.items():
        if event_key not in VALID_EVENT_KEYS:
            raise HTTPException(status_code=422, detail=f"Unknown event key '{event_key}'")
        if not isinstance(channels, dict):
            raise HTTPException(status_code=422, detail=f"'{event_key}' must map to channel flags")
        cleaned[event_key] = {
            k: bool(v) for k, v in channels.items() if k in ("in_app", "email")
        }
    user.notification_prefs = cleaned
    await db.commit()
    await db.refresh(user)
    # Echo effective prefs (defaults ⊕ overrides) so the UI renders truth
    from app.services.notifications import effective_prefs

    return {
        "prefs": {key: effective_prefs(user, key) for key in DEFAULT_PREFS},
        "email_enabled": bool(getattr(get_settings(), "email_enabled", False)),
    }


@router.get("/me/notification-prefs")
async def get_notification_prefs(user: User = Depends(get_current_user)) -> dict:
    from app.services.notifications import DEFAULT_PREFS, effective_prefs

    return {
        "prefs": {key: effective_prefs(user, key) for key in DEFAULT_PREFS},
        "email_enabled": bool(getattr(get_settings(), "email_enabled", False)),
    }


@router.get("/me/usage")
async def get_my_usage(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """MTD model spend vs caps (cost-caps-alerts spec R7)."""
    from app.services.spend import usage_summary

    return await usage_summary(db, user)


@router.get("/me/stats", response_model=UserStats)
async def get_my_stats(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> UserStats:
    projects = await db.scalar(
        select(func.count()).select_from(Project).where(Project.user_id == user.id)
    )
    specs = await db.scalar(
        select(func.count())
        .select_from(Spec)
        .join(Project, Spec.project_id == Project.id)
        .where(Project.user_id == user.id)
    )
    sessions = await db.scalar(
        select(func.count()).select_from(ChatSession).where(ChatSession.user_id == user.id)
    )
    deployments = await db.scalar(
        select(func.count()).select_from(Deployment).where(Deployment.user_id == user.id)
    )
    return UserStats(
        projects=projects or 0, specs=specs or 0, sessions=sessions or 0, deployments=deployments or 0
    )


# ------------------------------------------------------- MFA (S14-02, D8)


class MfaStartOut(BaseModel):
    secret: str
    otpauth_uri: str


class MfaConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


def _access_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return header.split(" ", 1)[1].strip()


@router.get("/me/mfa")
async def mfa_status(request: Request, user: User = Depends(get_current_user)) -> dict:
    """Enrollment state + whether THIS session used a second factor, so the UI
    can distinguish "not enrolled" from "enrolled but signed in before setup"."""
    from app.core.auth import session_mfa_satisfied
    from app.services import mfa as mfa_svc
    from app.services.platform_settings import get_security_controls

    claims = getattr(request.state, "auth_claims", None)
    try:
        state = await asyncio.to_thread(mfa_svc.status, _access_token(request))
    except mfa_svc.MfaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    controls = await get_security_controls()
    return {
        **state,
        "session_mfa": session_mfa_satisfied(claims, user),
        "required_for_admins": bool(controls.get("admin_mfa_required")),
        "required_for_me": bool(controls.get("admin_mfa_required")) and user.role == "admin",
    }


@router.post("/me/mfa/start", response_model=MfaStartOut)
async def mfa_start(request: Request, user: User = Depends(get_current_user)) -> MfaStartOut:
    from app.services import audit
    from app.services import mfa as mfa_svc

    try:
        data = await asyncio.to_thread(mfa_svc.start_enrollment, _access_token(request), user.email)
    except mfa_svc.MfaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Never audit the secret itself — only that enrollment began.
    audit.set_audit_detail(request, action_detail="mfa_enrollment_started")
    return MfaStartOut(**data)


@router.post("/me/mfa/confirm")
async def mfa_confirm(
    payload: MfaConfirmIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from datetime import UTC, datetime

    from app.services import audit
    from app.services import mfa as mfa_svc

    try:
        await asyncio.to_thread(mfa_svc.confirm_enrollment, _access_token(request), payload.code)
    except mfa_svc.MfaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # S14-02 fix (FSD §13.5M): record the confirmation instant — the MFA gate
    # treats sessions authenticated at/after this moment as second-factor
    # backed (Cognito access tokens carry no amr claim to read instead).
    user.mfa_enrolled_at = datetime.now(UTC)
    await db.commit()
    audit.set_audit_detail(request, action_detail="mfa_enabled")
    return {
        "enrolled": True,
        "message": "Authenticator app enabled. It applies to your next sign-in.",
    }


@router.delete("/me/mfa")
async def mfa_disable(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Self-service removal. Admins cannot remove their factor while the
    platform requires MFA for admins — that would be a self-lockout."""
    from app.services import audit
    from app.services import mfa as mfa_svc
    from app.services.platform_settings import get_security_controls

    if user.role == "admin" and (await get_security_controls()).get("admin_mfa_required"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "admin_mfa_required",
                "detail": "Multi-factor authentication is required for administrators "
                "and cannot be removed while that policy is on.",
            },
        )
    try:
        await asyncio.to_thread(mfa_svc.disable, _access_token(request))
    except mfa_svc.MfaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user.mfa_enrolled_at = None  # gate reverts: future sessions are not MFA-backed
    await db.commit()
    audit.set_audit_detail(request, action_detail="mfa_disabled")
    return {"enrolled": False}


@router.get("/me/deployments")
async def get_my_deployments(
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cross-project deployments list (the nav's Deployments page).

    Live defect 17 Sep 2026: the shell's "Deployments" item pointed at
    /projects since S15 — a label, not a page. This backs the real page.
    Visibility = the workbench/projects-list clause (owned ∪ explicitly
    shared ∪ team); the aggregate never shows more than the project pages
    would (B14 R1.2 discipline). Newest first, capped, name-joined.
    """
    from sqlalchemy import or_

    from app.core.tenant import tenant_scope
    from app.services import collab
    from app.services import teams as teams_svc

    member_roles = await collab.member_project_ids(db, user.id)
    team_ids = list((await teams_svc.memberships_for_user(db, user.id)).keys())
    clauses = [Project.user_id == user.id]
    if member_roles:
        clauses.append(Project.id.in_(member_roles.keys()))
    if team_ids:
        clauses.append(Project.team_id.in_(team_ids))
    visible = tenant_scope(
        select(Project.id).where(
            or_(*clauses) if len(clauses) > 1 else clauses[0],
            Project.status != "deleted",
        ),
        Project,
    ).scalar_subquery()

    rows = await db.execute(
        select(Deployment, Project.name)
        .join(Project, Deployment.project_id == Project.id)
        .where(Deployment.project_id.in_(visible))
        .order_by(Deployment.created_at.desc())
        .limit(50)
    )

    def _iso(dt):
        return dt.isoformat() if dt is not None else None

    deployments = [
        {
            "id": str(d.id),
            "project_id": str(d.project_id),
            "project_name": name,
            "status": d.status,
            "health": d.health,
            "mode": d.mode,
            "app_url": d.app_url,
            "mine": d.user_id == user.id,
            "deployed_at": _iso(d.deployed_at),
            "expires_at": _iso(d.expires_at),
            "torn_down_at": _iso(d.torn_down_at),
            "created_at": _iso(d.created_at),
        }
        for d, name in rows.all()
    ]
    return {"deployments": deployments}


@router.get("/me/workbench")
async def get_my_workbench(
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Home workbench aggregate (beta-usability spec R1 / FSD B14).

    What NEEDS this user, assembled from existing queries behind their
    existing access rules — the aggregate must never show more than the
    corresponding list surface would (R1.2). Sequential queries on the one
    request session (an AsyncSession is not concurrency-safe); every select
    is small, capped and index-served.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import or_

    from app.core.tenant import tenant_scope
    from app.models import AiRiskAssessment, Alert, CodegenBuild
    from app.services import collab, risk_review
    from app.services import teams as teams_svc

    now = datetime.now(UTC)

    def _aware(dt):
        """sqlite returns naive datetimes — normalize before arithmetic."""
        return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt

    # ---- reviews I owe (S6 queue — live-project filter rides along)
    groups = await risk_review.user_group_memberships(db, user.id)
    is_reviewer = bool(groups) or user.role == "admin"
    pending = len(await risk_review.queue_for_user(db, user)) if is_reviewer else 0

    # ---- my deployments expiring within 24h (owner-scoped: the ask is
    # "extend or let it die", which only the deployer answers)
    rows = await db.execute(
        tenant_scope(
            select(Deployment).where(
                Deployment.user_id == user.id,
                Deployment.status == "active",
                Deployment.expires_at.is_not(None),
                Deployment.expires_at <= now + timedelta(hours=24),
            ),
            Deployment,
        ).order_by(Deployment.expires_at.asc())
    )
    expiring = list(rows.scalars())

    # ---- failed builds, last 7d, across projects I can SEE (the projects-
    # list clause: owned ∪ explicitly shared ∪ team; the governance-viewer
    # carve-out is deliberately NOT part of list visibility)
    member_roles = await collab.member_project_ids(db, user.id)
    team_ids = list((await teams_svc.memberships_for_user(db, user.id)).keys())
    clauses = [Project.user_id == user.id]
    if member_roles:
        clauses.append(Project.id.in_(member_roles.keys()))
    if team_ids:
        clauses.append(Project.team_id.in_(team_ids))
    visible = tenant_scope(
        select(Project.id).where(
            or_(*clauses) if len(clauses) > 1 else clauses[0],
            Project.status != "deleted",
        ),
        Project,
    ).scalar_subquery()
    rows = await db.execute(
        select(CodegenBuild)
        .where(
            CodegenBuild.project_id.in_(visible),
            CodegenBuild.status == "failed",
            CodegenBuild.created_at >= now - timedelta(days=7),
        )
        .order_by(CodegenBuild.created_at.desc())
        .limit(5)
    )
    failed_builds = list(rows.scalars())

    # ---- owned projects whose LATEST assessment requests changes. Decision
    # rows are terminal (a resubmission is a NEW row), so "awaiting" means
    # last-write-wins per project — the projects.py risk-level precedent.
    owned_live = (
        select(Project.id).where(Project.user_id == user.id, Project.status != "deleted")
    ).scalar_subquery()
    rows = await db.execute(
        select(AiRiskAssessment)
        .where(
            AiRiskAssessment.project_id.in_(owned_live),
            AiRiskAssessment.decision.is_not(None),
        )
        .order_by(AiRiskAssessment.created_at.asc())
    )
    latest_by_project: dict = {}
    for assessment in rows.scalars():
        latest_by_project[assessment.project_id] = assessment  # ascending → last wins
    awaiting = [
        a for a in latest_by_project.values() if a.decision == "changes_requested"
    ][:5]

    # ---- batch project names for every row we will render (no N+1)
    project_ids = (
        {d.project_id for d in expiring}
        | {b.project_id for b in failed_builds}
        | {a.project_id for a in awaiting}
    )
    names: dict = {}
    if project_ids:
        name_rows = await db.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        names = dict(name_rows.all())

    payload: dict = {
        "reviews": {"is_reviewer": is_reviewer, "pending": pending},
        "expiring_deployments": [
            {
                "project_id": str(d.project_id),
                "project_name": names.get(d.project_id),
                "app_url": d.app_url,
                "expires_at": _aware(d.expires_at).isoformat(),
                "hours_left": round(
                    (_aware(d.expires_at) - now).total_seconds() / 3600, 1
                ),
            }
            for d in expiring
        ],
        "failed_builds": [
            {
                "project_id": str(b.project_id),
                "project_name": names.get(b.project_id),
                "build_id": str(b.id),
                "error_code": (b.error or {}).get("code"),
                "created_at": _aware(b.created_at).isoformat(),
            }
            for b in failed_builds
        ],
        "awaiting_resubmission": [
            {
                "project_id": str(a.project_id),
                "project_name": names.get(a.project_id),
                "level": a.level,
                "notes": a.notes,
                "decided_at": _aware(a.decided_at).isoformat() if a.decided_at else None,
            }
            for a in awaiting
        ],
    }

    # ---- ops line (admin only — R1.4)
    if user.role == "admin":
        severity_rows = await db.execute(
            tenant_scope(
                select(Alert.severity).where(Alert.status == "active"), Alert
            )
        )
        severities = [r[0] for r in severity_rows.all()]
        rank = {"info": 0, "warning": 1, "critical": 2}
        payload["ops"] = {
            "active_alerts": len(severities),
            "max_severity": max(severities, key=lambda s: rank.get(s, 0)) if severities else None,
        }
    return payload
