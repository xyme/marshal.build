"""Admin governance APIs: model controls, risk queue, cost dashboard
(cost-risk-governance spec R1/R3/R4/R5)."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.models import AiRiskAssessment, PlatformSettings, Project, User
from app.services import audit
from app.services import costs as costs_svc
from app.services import model_endpoints as endpoints_svc
from app.services import platform_settings as settings_svc
from app.services import risk as risk_svc

logger = logging.getLogger("marshal.admin_governance")

router = APIRouter(
    prefix="/admin",
    tags=["admin-governance"],
    dependencies=[Depends(require_role("admin"))],
)


# ------------------------------------------------------------- model controls


class ModelControlsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_allowlist: list[str]
    param_bounds: dict
    rate_limits: dict
    cost: dict
    codegen: dict = {}
    deployment_policies: dict = {}
    security: dict = {}
    feature_flags: dict = {}
    updated_by: uuid.UUID | None = None
    updated_at: datetime | None = None


class ModelControlsIn(BaseModel):
    model_allowlist: list[str] = Field(min_length=1)
    param_bounds: dict
    rate_limits: dict = {}
    cost: dict = {}
    codegen: dict = {}
    deployment_policies: dict | None = None
    security: dict | None = None
    feature_flags: dict | None = None  # S18: merged (omitted flags survive)


@router.get("/model-controls", response_model=ModelControlsOut)
async def get_model_controls(db: AsyncSession = Depends(get_db)) -> ModelControlsOut:
    row = await db.get(PlatformSettings, 1)
    if row is None:
        raise HTTPException(status_code=404, detail="Platform settings not initialized")
    return ModelControlsOut.model_validate(row)


@router.put("/model-controls", response_model=ModelControlsOut)
async def put_model_controls(
    payload: ModelControlsIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModelControlsOut:
    before = await db.get(PlatformSettings, 1)
    before_snapshot = (
        {
            "model_allowlist": before.model_allowlist,
            "param_bounds": before.param_bounds,
            "rate_limits": before.rate_limits,
            "cost": before.cost,
            "codegen": before.codegen,
            "deployment_policies": getattr(before, "deployment_policies", None),
            "security": getattr(before, "security", None),
            "feature_flags": getattr(before, "feature_flags", None),
        }
        if before
        else None
    )
    try:
        row = await settings_svc.update_controls(
            db,
            model_allowlist=payload.model_allowlist,
            param_bounds=payload.param_bounds,
            rate_limits=payload.rate_limits,
            cost=payload.cost,
            codegen=payload.codegen,
            deployment_policies=payload.deployment_policies,
            security=payload.security,
            feature_flags=payload.feature_flags,
            updated_by=admin.id,
        )
    except settings_svc.SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, before=before_snapshot, after=payload.model_dump())
    return ModelControlsOut.model_validate(row)


# ------------------------------------------------------------- risk policy (B17)


@router.get("/governance/risk-policy")
async def get_risk_policy(db: AsyncSession = Depends(get_db)) -> dict:
    """Effective risk policy + version (B17 R3.1). version 1 = platform
    defaults; the blob may be empty and this still answers."""
    return risk_svc.policy_out(await risk_svc.resolve_risk_policy(db))


@router.put("/governance/risk-policy")
async def put_risk_policy(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Partial policy update (B17 R3.1/R3.2). Bumps policy_version exactly
    once iff the effective policy changed; every accepted change is an ADMIN
    audit event with before/after. Consequence by design: assessments under
    the old version stop opening the gate — next deploy re-scores."""
    before = risk_svc.policy_out(await risk_svc.resolve_risk_policy(db))
    try:
        after_policy = await risk_svc.update_risk_policy(db, payload or {})
    except risk_svc.RiskPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    after = risk_svc.policy_out(after_policy)
    audit.set_audit_detail(
        request,
        before={k: before[k] for k in ("policy_version", "auto_approve_low", "band_low_max", "band_medium_max", "weights", "anchors")},
        after={k: after[k] for k in ("policy_version", "auto_approve_low", "band_low_max", "band_medium_max", "weights", "anchors")},
        version_bumped=after["policy_version"] != before["policy_version"],
    )
    return after


@router.get("/codegen-health")
async def codegen_health() -> dict:
    """Runner reachability for the Model Controls codegen card (S9 R6.3)."""
    return await _codegen_health_impl()


@router.get("/email/status")
async def email_status() -> dict:
    """Email channel health (S15-07): flag, SES prod access, DKIM, suppression
    list. This is the operator's answer to 'why did no mail arrive?'."""
    import asyncio

    from app.services.notifications import email_channel_status

    return await asyncio.to_thread(email_channel_status)


# ----------------------------------------------- S17: custom model endpoints


class EndpointIn(BaseModel):
    slug: str = Field(min_length=3, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=9, max_length=512)
    model_name: str = Field(min_length=1, max_length=120)
    tier: str = Field(default="standard", pattern="^(standard|fast|advanced)$")
    usd_per_1k_input: float
    usd_per_1k_output: float
    max_context_tokens: int = 8192
    timeout_s: int = 60
    api_key: str | None = Field(default=None, max_length=4096)


class EndpointUpdateIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=9, max_length=512)
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    tier: str | None = Field(default=None, pattern="^(standard|fast|advanced)$")
    usd_per_1k_input: float | None = None
    usd_per_1k_output: float | None = None
    max_context_tokens: int | None = None
    timeout_s: int | None = None
    enabled: bool | None = None
    api_key: str | None = Field(default=None, max_length=4096)


@router.get("/model-endpoints")
async def list_model_endpoints(db: AsyncSession = Depends(get_db)) -> list[dict]:
    return await endpoints_svc.list_endpoints(db)


@router.post("/model-endpoints", status_code=201)
async def create_model_endpoint(
    payload: EndpointIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        created = await endpoints_svc.create_endpoint(
            db, payload.model_dump(), admin.id
        )
    except endpoints_svc.EndpointValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, slug=created["slug"], base_url=created["base_url"],
        has_api_key=created["has_api_key"],
    )
    return created


@router.put("/model-endpoints/{slug}")
async def update_model_endpoint(
    slug: str,
    payload: EndpointUpdateIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        updated, change = await endpoints_svc.update_endpoint(
            db, slug, payload.model_dump(exclude_none=True), admin.id
        )
    except endpoints_svc.EndpointNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except endpoints_svc.EndpointValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, **change)
    return updated


@router.post("/model-endpoints/{slug}/probe")
async def probe_model_endpoint(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Is it up NOW — 1-token completion + streaming check, never cached."""
    row = await db.get(PlatformSettings, 1)
    entries = list(getattr(row, "custom_model_endpoints", None) or []) if row else []
    entry = next((e for e in entries if e.get("slug") == slug), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No custom endpoint '{slug}'")
    try:
        result = await endpoints_svc.probe(slug, entry)
    except endpoints_svc.ProbeBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, slug=slug, ok=result.get("ok"), rtt_ms=result.get("rtt_ms"),
        error_class=result.get("error_class"),
    )
    return result


async def _codegen_health_impl() -> dict:
    from app.core.config import get_settings
    from app.services.codegen.provider import resolve_provider_name
    from app.services.codegen.workspace_runner import WorkspaceRunnerProvider

    active = await resolve_provider_name()
    settings = get_settings()
    if not settings.codegen_workspace_bucket:
        return {
            "active_provider": active,
            "runner": {"reachable": False, "detail": "workspace bucket not configured"},
        }
    try:
        provider = WorkspaceRunnerProvider()
        return {"active_provider": active, "runner": await provider.health()}
    except Exception as exc:  # noqa: BLE001
        return {"active_provider": active, "runner": {"reachable": False, "detail": str(exc)[:200]}}


# --------------------------------------------------------------------- risk


class RiskAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    project_name: str | None = None
    owner_email: str | None = None
    content_hash: str
    rubric_version: int
    score: int | None
    level: str | None
    factors: dict
    status: str
    decision: str | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    notes: str | None
    error: str | None = None
    created_at: datetime
    assigned_group: str | None = None
    escalated_to: str | None = None
    routed_at: datetime | None = None
    resubmission_of: uuid.UUID | None = None
    owner_comment: str | None = None


class RiskListOut(BaseModel):
    items: list[RiskAssessmentOut]
    total: int
    pending: int
    approved_30d: int
    rejected_30d: int
    page: int
    page_size: int


class RiskDecideIn(BaseModel):
    # Back-compat: {approve: bool}; S6 adds outcome (request_changes)
    approve: bool | None = None
    outcome: str | None = Field(default=None, pattern="^(approve|reject|request_changes)$")
    notes: str | None = Field(default=None, max_length=2000)

    def effective_outcome(self) -> str:
        if self.outcome:
            return self.outcome
        if self.approve is None:
            raise ValueError("Provide either 'outcome' or 'approve'")
        return "approve" if self.approve else "reject"


async def _risk_out(db: AsyncSession, row: AiRiskAssessment) -> RiskAssessmentOut:
    out = RiskAssessmentOut.model_validate(row)
    project = await db.get(Project, row.project_id)
    if project:
        out.project_name = project.name
        owner = await db.get(User, project.user_id)
        out.owner_email = owner.email if owner else None
    return out


async def _risk_out_page(
    db: AsyncSession, rows: list[AiRiskAssessment]
) -> list[RiskAssessmentOut]:
    """Page-scoped enrichment in two grouped queries (S13-02).

    The per-row variant issued 2 lookups each (project, then its owner) — the
    slowest read surface in the S13 baseline. Same output, no N+1.
    """
    items = [RiskAssessmentOut.model_validate(row) for row in rows]
    project_ids = {row.project_id for row in rows if row.project_id}
    if not project_ids:
        return items
    projects = {
        p.id: p
        for p in (
            await db.execute(select(Project).where(Project.id.in_(project_ids)))
        ).scalars()
    }
    owner_ids = {p.user_id for p in projects.values() if p.user_id}
    emails: dict = {}
    if owner_ids:
        emails = dict(
            (await db.execute(select(User.id, User.email).where(User.id.in_(owner_ids)))).all()
        )
    for out, row in zip(items, rows, strict=True):
        project = projects.get(row.project_id)
        if project is not None:
            out.project_name = project.name
            out.owner_email = emails.get(project.user_id)
    return items


@router.get("/risk-assessments", response_model=RiskListOut)
async def list_risk_assessments(
    status: str | None = Query("pending", pattern="^(pending|approved|rejected|changes_requested|auto_approved|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> RiskListOut:
    stmt = select(AiRiskAssessment)
    if status and status != "all":
        stmt = stmt.where(AiRiskAssessment.decision == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(
        stmt.order_by(AiRiskAssessment.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = await _risk_out_page(db, list(result.scalars()))
    counters = await risk_svc.queue_counters(db)
    return RiskListOut(items=items, total=total, page=page, page_size=page_size, **counters)


@router.get("/risk-assessments/export")
async def export_risk_decisions(db: AsyncSession = Depends(get_db)):
    """CSV of decisions (risk-review-workflow spec R5.3)."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    rows = (
        await db.execute(
            select(AiRiskAssessment)
            .where(AiRiskAssessment.decision.is_not(None))
            .order_by(AiRiskAssessment.created_at.desc())
            .limit(10_000)
        )
    ).scalars().all()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "created_at", "project_id", "score", "level", "decision",
            "assigned_group", "escalated_to", "routed_at", "decided_at",
            "decided_by", "notes",
        ])
        for a in rows:
            writer.writerow([
                a.created_at.isoformat(), a.project_id, a.score, a.level, a.decision,
                a.assigned_group or "", a.escalated_to or "",
                a.routed_at.isoformat() if a.routed_at else "",
                a.decided_at.isoformat() if a.decided_at else "",
                a.decided_by or "", (a.notes or "").replace("\n", " "),
            ])
        yield buf.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="marshal-risk-decisions.csv"'},
    )


@router.get("/risk-assessments/{assessment_id}", response_model=RiskAssessmentOut)
async def get_risk_assessment(
    assessment_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> RiskAssessmentOut:
    row = await db.get(AiRiskAssessment, assessment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return await _risk_out(db, row)


@router.post("/risk-assessments/{assessment_id}/decide", response_model=RiskAssessmentOut)
async def decide_risk_assessment(
    assessment_id: uuid.UUID,
    payload: RiskDecideIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RiskAssessmentOut:
    row = await db.get(AiRiskAssessment, assessment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    from app.services import risk_review

    try:
        outcome = payload.effective_outcome()
        row = await risk_review.decide(db, row, admin, outcome=outcome, notes=payload.notes)
    except (risk_review.ReviewError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request,
        project_id=str(row.project_id),
        outcome=outcome,
        score=row.score,
        level=row.level,
    )
    return await _risk_out(db, row)


# ------------------------------------------------------------ reviewer groups


class GroupMembersIn(BaseModel):
    user_ids: list[uuid.UUID]


@router.get("/reviewer-groups")
async def get_reviewer_groups(db: AsyncSession = Depends(get_db)) -> dict:
    from app.services import risk_review

    result: dict = {"groups": {}, "counters": await risk_review.summary_counters(db)}
    for group in risk_review.VALID_GROUPS:
        member_ids = await risk_review.group_member_ids(db, group)
        members = []
        for user_id in member_ids:
            user = await db.get(User, user_id)
            if user:
                pending = len(await risk_review.queue_for_user(db, user))
                members.append(
                    {"id": str(user.id), "email": user.email, "name": user.name,
                     "pending": pending}
                )
        result["groups"][group] = members
    return result


@router.put("/reviewer-groups/{group_name}")
async def put_reviewer_group(
    group_name: str,
    payload: GroupMembersIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.services import risk_review

    try:
        member_ids = await risk_review.set_group_members(
            db, group_name, payload.user_ids, admin
        )
    except risk_review.ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, group=group_name, member_count=len(member_ids))
    return {"group": group_name, "user_ids": [str(u) for u in member_ids]}


# --------------------------------------------------------------------- alerts


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    severity: str
    threshold_pct: int | None
    message: str
    status: str
    created_at: datetime
    acknowledged_at: datetime | None


@router.get("/alerts")
async def list_alerts(
    status: str = Query("active", pattern="^(active|acknowledged|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Alert

    stmt = select(Alert)
    if status != "all":
        stmt = stmt.where(Alert.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(
        stmt.order_by(Alert.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "items": [AlertOut.model_validate(a).model_dump(mode="json") for a in result.scalars()],
        "total": total, "page": page, "page_size": page_size,
    }


@router.put("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: uuid.UUID,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Alert

    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    if alert.status != "acknowledged":
        alert.status = "acknowledged"
        alert.acknowledged_by = admin.id
        alert.acknowledged_at = datetime.now(UTC)
        await db.commit()
    audit.set_audit_detail(request, kind=alert.kind, message=alert.message)
    return AlertOut.model_validate(alert).model_dump(mode="json")


# --------------------------------------------------------------------- costs


@router.get("/costs")
async def get_costs(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import Alert, SandboxSpend

    data = await costs_svc.dashboard(db, period)
    active = await db.execute(
        select(Alert).where(Alert.status == "active").order_by(Alert.created_at.desc()).limit(10)
    )
    data["alerts"] = [
        AlertOut.model_validate(a).model_dump(mode="json") for a in active.scalars()
    ]
    sandbox_total = (
        await db.execute(select(func.coalesce(func.sum(SandboxSpend.usd), 0)))
    ).scalar_one()
    data["sandbox_spend_usd"] = float(sandbox_total) if sandbox_total else None
    return data


@router.get("/costs/breakdown")
async def get_costs_breakdown(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    group_by: str = Query("user", pattern="^(user|project|team|model|purpose|day)$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await costs_svc.breakdown(db, period, group_by)


@router.get("/costs/teams")
async def get_team_costs(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-team budget vs spend (5 Sep 2026) — visibility, not enforcement."""
    return await costs_svc.team_costs(db, period)


@router.get("/costs/chargeback")
async def get_chargeback(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    months: int = Query(6, ge=1, le=12),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
):
    """Cost allocation by user/project/team + monthly rollups (S16-03).

    Model spend is always live; Enclave infra spend folds in when Cost
    Explorer is enabled [D3] — the response says which story it is telling.
    """
    report = await costs_svc.chargeback(db, period, months=months)
    if format == "csv":
        from fastapi.responses import PlainTextResponse

        filename = f"marshal-chargeback-{report['period']}.csv"
        return PlainTextResponse(
            costs_svc.chargeback_csv(report),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return report
