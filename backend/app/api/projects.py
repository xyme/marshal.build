"""Projects API — F-007: creation (S1), dashboard/lifecycle/activity (S3-07).

Lifecycle per FSD §4.7.2: archive from any state except building; soft delete
(status='deleted', hidden everywhere) blocked while a deployment is active.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import project_owner, project_viewer
from app.core.auth import get_current_user, require_editor_persona
from app.core.db import get_db
from app.models import (
    AuditLog,
    ChatSession,
    Deployment,
    Lease,
    MarketplaceSample,
    Project,
    Spec,
    Template,
    User,
)
from app.schemas.projects import (
    ActivityItemOut,
    ActivityListOut,
    DeploymentOut,
    ProjectCreate,
    ProjectImportIn,
    ProjectListOut,
    ProjectOut,
    ProjectUpdate,
)
from app.services import audit
from app.services import project_import as import_svc
from app.services.capabilities import CapabilityNotAllowed
from app.services.composition import CompositionError

router = APIRouter(prefix="/projects", tags=["projects"])

ACTIVE_DEPLOYMENT_STATUSES = ("pending", "pre_flight", "deploying", "active", "tearing_down")
SORTS = {
    "recent": Project.updated_at.desc(),
    "name": Project.name.asc(),
    "created": Project.created_at.desc(),
}
SPEC_ACTIONS = (
    "spec_saved", "spec_rolled_back", "spec_draft_saved",
    "spec_draft_discarded", "session_spec_saved",
)


# Access seam (collaboration spec R1): endpoints declare min role via
# app.api.deps.require_project_role — the old owner-or-admin lookup is gone.
# Owner-gated routes below use project_owner; read surfaces use project_viewer.


async def latest_deployment(db: AsyncSession, project_id: uuid.UUID) -> Deployment | None:
    result = await db.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


_lease_state_cache: dict[str, tuple[float, dict | None]] = {}
LEASE_STATE_TTL_S = 300


async def deployment_out(
    db: AsyncSession, deployment: Deployment | None, *, with_lease_state: bool = False
) -> DeploymentOut | None:
    if deployment is None:
        return None
    out = DeploymentOut.model_validate(deployment)
    if deployment.lease_id:
        lease = await db.get(Lease, deployment.lease_id)
        if lease:
            out.lease_account_id = lease.aws_account_id
            out.lease_external_id = lease.external_lease_id
            # S11 R6: ISB lease economics, cached 5 min, best-effort
            if with_lease_state and lease.external_lease_id and deployment.status == "active":
                import time

                from app.services.deployment import provider_for_lease

                cached = _lease_state_cache.get(lease.external_lease_id)
                if cached and time.monotonic() - cached[0] < LEASE_STATE_TTL_S:
                    out.lease_state = cached[1]
                else:
                    try:
                        # B20 R0.3: ask the provider that OWNS this lease
                        state = await provider_for_lease(lease).lease_state(
                            lease.external_lease_id
                        )
                    except Exception:  # noqa: BLE001 — graceful degrade (R6.2)
                        state = None
                    _lease_state_cache[lease.external_lease_id] = (time.monotonic(), state)
                    out.lease_state = state
    return out


async def _project_out(db: AsyncSession, project: Project, *, with_deployment: bool = True) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    if project.template_id:
        template = await db.get(Template, project.template_id)
        if template:
            out.template_name = template.name
            out.template_deprecated = template.status == "deprecated"
    if project.forked_from_sample_id:
        sample = await db.get(MarketplaceSample, project.forked_from_sample_id)
        if sample:
            out.forked_from_title = sample.title
    if project.team_id:  # S15-02 team badge
        from app.models import Team

        team = await db.get(Team, project.team_id)
        if team:
            out.team_name = team.name
    if project.composition and (project.composition.get("dependencies") or []):
        # Composable agents R1.2: the graph renders with LIVE dependency
        # deployment status — one grouped query, no N+1.
        deps = [dict(d) for d in project.composition["dependencies"]]
        dep_ids = []
        for d in deps:
            try:
                dep_ids.append(uuid.UUID(str(d.get("project_id"))))
            except (ValueError, TypeError):
                continue
        active_ids: set = set()
        if dep_ids:
            rows = await db.execute(
                select(Deployment.project_id).where(
                    Deployment.project_id.in_(dep_ids),
                    Deployment.status == "active",
                )
            )
            active_ids = {str(r) for r in rows.scalars()}
        for d in deps:
            d["deployment_status"] = (
                "active" if str(d.get("project_id")) in active_ids else "not_deployed"
            )
        out.composition = {**project.composition, "dependencies": deps}
    if with_deployment:
        out.deployment = await deployment_out(db, await latest_deployment(db, project.id))
    return out


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if user.kind == "service":
        # B9 act-only (integration-wave R2.3): service accounts operate on
        # projects shared WITH them; a human owns every project.
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot own projects — have a human owner "
            "create it and share it with this account.",
        )
    template = None
    if payload.template_id:
        template = await db.get(Template, payload.template_id)
        if template is None or template.status != "active":
            raise HTTPException(status_code=422, detail="Template not found or not active")
    project = Project(
        user_id=user.id,
        name=payload.name,
        description=payload.description,
        template_id=template.id if template else None,
        origin="template" if template else "scratch",
    )
    db.add(project)
    if template:
        template.usage_count = (template.usage_count or 0) + 1  # S2-05 R3.5
    await db.commit()
    await db.refresh(project)
    # No project_id path param on create — enrich so the activity tab sees it (R3.1)
    audit.set_audit_detail(
        request, project_id=str(project.id), resource_id=str(project.id), name=project.name
    )
    return await _project_out(db, project)


@router.post("/import", response_model=ProjectOut, status_code=201)
async def import_project(
    payload: ProjectImportIn,
    request: Request,
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """External spec-set import (I1/I2) — template-less, fork-shaped, governed.

    Editor-persona bar: raw markdown import is Power-User surface area, the
    same line spec editing draws. Imported content flows through the identical
    risk/conformance/deploy gates as authored content (provenance-blind).
    """
    if user.kind == "service":
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot import projects — have a human owner "
            "import it and share it with this account.",
        )
    provided = [
        s
        for s in ("docs", "archive_b64", "url", "document_b64")
        if getattr(payload, s, None)
    ]
    if len(provided) != 1:
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of docs, archive_b64, url or document_b64",
        )
    try:
        if payload.url:
            # I3 v1 (owner resolution 7 Sep 2026): public https fetch,
            # SSRF-filtered — the source label defaults to the URL itself.
            content, content_type = await import_svc.fetch_public_url(payload.url)
            docs = import_svc.docs_from_fetched(content, content_type)
        elif payload.archive_b64:
            docs = import_svc.parse_archive(payload.archive_b64)
        elif payload.document_b64:
            # pdf-docx-ingestion: extraction is CPU-bound — off the event loop.
            docs = await asyncio.to_thread(
                import_svc.parse_document, payload.document_b64
            )
        else:
            docs = dict(payload.docs or {})
        source = payload.source or (
            payload.url[:120]
            if payload.url
            else (payload.document_name or "")[:120] or None
        )
        project, imported = await import_svc.import_spec_set(
            db, user, name=payload.name, docs=docs, source=source
        )
    except import_svc.ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CapabilityNotAllowed as exc:
        # Agent substance R3.2: imports honor the template rail by name too
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    except CompositionError as exc:
        # Composable agents R1.2: imports refuse invalid graphs by name too
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    audit.set_audit_detail(
        request,
        project_id=str(project.id),
        resource_id=str(project.id),
        name=project.name,
        source=source,
        source_url=payload.url,  # I3 provenance (None for docs/zip imports)
        document_name=payload.document_name,  # pdf-docx provenance, never content
        docs=imported,
        sizes={t: len(docs.get(t) or "") for t in imported},
    )
    return await _project_out(db, project)


@router.get("", response_model=ProjectListOut)
async def list_projects(
    q: str | None = None,
    status: str | None = Query(
        None,
        pattern="^(draft|spec_complete|building|deployed|inactive|archived)$",
    ),
    sort: str = Query("recent", pattern="^(recent|name|created)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectListOut:
    from sqlalchemy import or_

    from app.services import collab

    # Owned ∪ member projects (collaboration spec R2.3); my_role annotated below
    member_roles = await collab.member_project_ids(db, user.id)
    # S15-02: owned ∪ explicitly-shared ∪ projects of teams the user belongs to
    from app.services import teams as teams_svc

    memberships = await teams_svc.memberships_for_user(db, user.id)
    team_ids = list(memberships.keys())
    team_roles = {
        team_id: teams_svc.TEAM_ROLE_TO_PROJECT_ROLE.get(role)
        for team_id, role in memberships.items()
    }
    clauses = [Project.user_id == user.id]
    if member_roles:
        clauses.append(Project.id.in_(member_roles.keys()))
    if team_ids:
        clauses.append(Project.team_id.in_(team_ids))
    ownership = or_(*clauses) if len(clauses) > 1 else clauses[0]
    from app.core.tenant import tenant_scope

    base = tenant_scope(
        select(Project).where(ownership, Project.status != "deleted"), Project
    )
    if q:
        base = base.where(Project.name.ilike(f"%{q}%"))
    stmt = base.where(
        Project.status == status if status else Project.status != "archived"
    )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    archived_count = (
        await db.execute(
            select(func.count()).select_from(
                base.where(Project.status == "archived").subquery()
            )
        )
    ).scalar_one()

    result = await db.execute(
        stmt.order_by(SORTS[sort]).offset((page - 1) * page_size).limit(page_size)
    )
    projects = list(result.scalars())

    # Page-scoped aggregates (two grouped queries, no N+1)
    ids = [p.id for p in projects]
    version_counts: dict[uuid.UUID, int] = {}
    last_spec_at: dict[uuid.UUID, datetime] = {}
    last_session_at: dict[uuid.UUID, datetime] = {}
    if ids:
        rows = await db.execute(
            select(Spec.project_id, func.count(), func.max(Spec.created_at))
            .where(Spec.project_id.in_(ids))
            .group_by(Spec.project_id)
        )
        for pid, count, latest in rows.all():
            version_counts[pid] = count
            last_spec_at[pid] = latest
        rows = await db.execute(
            select(ChatSession.project_id, func.max(ChatSession.updated_at))
            .where(ChatSession.project_id.in_(ids))
            .group_by(ChatSession.project_id)
        )
        last_session_at = dict(rows.all())

    # Risk level (latest assessment) + model-spend MTD per page project (S4-01/02)
    risk_levels: dict[uuid.UUID, str] = {}
    cost_mtd: dict[uuid.UUID, float] = {}
    if ids:
        from app.models import AiRiskAssessment, ModelInvocation

        rows = await db.execute(
            select(AiRiskAssessment.project_id, AiRiskAssessment.level, AiRiskAssessment.created_at)
            .where(AiRiskAssessment.project_id.in_(ids), AiRiskAssessment.level.is_not(None))
            .order_by(AiRiskAssessment.created_at.asc())
        )
        for pid, level, _created in rows.all():
            risk_levels[pid] = level  # ascending order → last write wins = latest
        month_start = datetime.now(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        rows = await db.execute(
            select(ModelInvocation.project_id, func.sum(ModelInvocation.cost_usd))
            .where(
                ModelInvocation.project_id.in_(ids),
                ModelInvocation.created_at >= month_start,
                ModelInvocation.cost_usd.is_not(None),
            )
            .group_by(ModelInvocation.project_id)
        )
        cost_mtd = {pid: float(total_cost) for pid, total_cost in rows.all() if total_cost}

    items = []
    for project in projects:
        out = await _project_out(db, project)
        # S15-02: role may come from ownership, an explicit grant, or the team
        # the project is filed in — highest wins, mirroring collab.resolve_role.
        if project.user_id == user.id:
            out.my_role = "owner"
        else:
            candidates = [r for r in (member_roles.get(project.id), team_roles.get(project.team_id)) if r]
            out.my_role = (
                max(candidates, key=lambda r: collab.ROLE_ORDER.get(r, -1))
                if candidates
                else None
            )
        out.spec_version_count = version_counts.get(project.id, 0)
        out.risk_level = risk_levels.get(project.id)
        out.cost_mtd_usd = cost_mtd.get(project.id)
        candidates = [
            t for t in (
                project.updated_at,
                last_spec_at.get(project.id),
                last_session_at.get(project.id),
            ) if t is not None
        ]
        out.last_activity_at = max(candidates) if candidates else project.updated_at
        items.append(out)
    return ProjectListOut(
        items=items, total=total, archived_count=archived_count, page=page, page_size=page_size
    )


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    request: Request,
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    out = await _project_out(db, project)
    out.my_role = getattr(request.state, "project_role", None)
    return out


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    payload: ProjectUpdate,
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    before = {"name": project.name, "description": project.description}
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    await db.commit()
    await db.refresh(project)
    audit.set_audit_detail(
        request, before=before, after={"name": project.name, "description": project.description}
    )
    return await _project_out(db, project)


@router.post("/{project_id}/archive", response_model=ProjectOut)
async def archive_project(
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if project.status == "building":
        raise HTTPException(status_code=409, detail="Cannot archive while a build is in progress")
    if project.status == "archived":
        return await _project_out(db, project)
    audit.set_audit_detail(request, previous_status=project.status)
    project.status = "archived"
    project.archived_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)
    return await _project_out(db, project)


@router.post("/{project_id}/restore", response_model=ProjectOut)
async def restore_project(
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    if project.status != "archived":
        raise HTTPException(status_code=409, detail="Only archived projects can be restored")
    # Restore to the most meaningful pre-archive state we can infer (§4.7.9 AC-4:
    # data intact either way; status recomputed from what exists).
    has_specs = (
        await db.execute(
            select(func.count()).select_from(
                select(Spec).where(Spec.project_id == project.id).subquery()
            )
        )
    ).scalar_one() > 0
    deployment = await latest_deployment(db, project.id)
    if deployment and deployment.status == "active":
        project.status = "deployed"
    elif deployment and deployment.status == "torn_down":
        project.status = "inactive"
    else:
        project.status = "spec_complete" if has_specs else "draft"
    project.archived_at = None
    await db.commit()
    await db.refresh(project)
    return await _project_out(db, project)  # template_deprecated flag drives the UI banner


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> None:
    deployment = await latest_deployment(db, project.id)
    if deployment and deployment.status in ACTIVE_DEPLOYMENT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Tear down the active deployment before deleting this project",
        )
    audit.set_audit_detail(request, previous_status=project.status, name=project.name)
    project.status = "deleted"
    project.deleted_at = datetime.now(UTC)
    await db.commit()


@router.put("/{project_id}/team")
async def set_project_team(
    payload: dict,
    request: Request,
    project: Project = Depends(project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """File a project into a team workspace, or back to personal (S15-02).

    Owner-level because it changes WHO CAN SEE the project — an editing right
    should not be able to widen an audience.
    """
    from app.services import teams as teams_svc

    raw = payload.get("team_id")
    team_id = None
    if raw:
        try:
            team_id = uuid.UUID(str(raw))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="team_id must be a UUID") from exc
    before = str(project.team_id) if project.team_id else None
    try:
        project = await teams_svc.assign_project(db, project, team_id, user)
    except teams_svc.TeamError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, before={"team_id": before}, after={"team_id": str(team_id) if team_id else None}
    )
    return {"project_id": str(project.id), "team_id": str(team_id) if team_id else None}


@router.put("/{project_id}/budget")
async def set_project_budget(
    payload: dict,
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-project monthly budget (cost-caps-alerts spec R4)."""
    raw = payload.get("budget_override_usd")
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="budget_override_usd must be a number") from None
        if not 0 <= value <= 100_000:
            raise HTTPException(status_code=422, detail="budget must be between 0 and 100000")
    before = float(project.budget_override_usd) if project.budget_override_usd is not None else None
    project.budget_override_usd = raw if raw is None else float(raw)
    await db.commit()
    await db.refresh(project)
    after = float(project.budget_override_usd) if project.budget_override_usd is not None else None
    audit.set_audit_detail(request, before={"budget": before}, after={"budget": after})
    return {"project_id": str(project.id), "budget_override_usd": after}


@router.get("/{project_id}/export")
async def export_project_spec(
    request: Request,
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
):
    """Download the latest spec set as a KIRO-layout zip (S4-06)."""
    from fastapi.responses import Response

    from app.services import export as export_svc
    from app.services.exportlimit import check_export_rate

    check_export_rate(request.state.user.id)
    try:
        payload, filename, manifest = await export_svc.build_export_zip(db, project)
    except export_svc.NothingToExport:
        raise HTTPException(
            status_code=409, detail="Nothing to export — save a spec document first"
        ) from None
    # GETs skip the audit middleware; exports are audit-worthy (spec R1.3)
    user = getattr(request.state, "user", None)
    audit.emit(
        audit.write_entry(
            actor_id=user.id if user else None,
            category=audit.USER,
            action="spec_exported",
            resource_type="project",
            resource_id=str(project.id),
            project_id=project.id,
            detail={"documents": manifest["documents"], "filename": filename},
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            http_status=200,
        )
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/risk")
async def project_risk(
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Latest assessment + whether it matches the CURRENT spec content (S4-02 R2.6)."""
    from app.services import risk as risk_svc
    from app.services.policies import resolve_deployment_policies

    # B20 R1.3: gate stance is a property of the DEPLOY INTENT (the mode the
    # form selects), not of the project — expose the per-mode map + preselect
    # so the panel renders the truth instead of assuming enforce.
    deploy_policies = await resolve_deployment_policies(db)
    mode_context = {
        "gate_modes": {
            "full_governance": "enforce",
            "testbed": (
                deploy_policies.testbed_gate_mode
                if deploy_policies.testbed_enabled
                else "disabled"
            ),
        },
        "default_mode": deploy_policies.default_mode,
    }

    # B17: surface policy currency alongside content currency — an assessment
    # scored under a superseded policy no longer opens the gate (re-scored on
    # next deploy), and the UI shows a "policy vN (superseded)" chip.
    risk_policy = await risk_svc.resolve_risk_policy(db)
    docs = await risk_svc._latest_docs(db, project.id)  # noqa: SLF001 — same-package seam
    from app.services.codegen.validate import endpoint_auth_decision

    auth = endpoint_auth_decision(docs.get("requirements", ""))
    auth_context = {
        "endpoint_auth": auth["mode"],
        "endpoint_auth_source": auth["source"],
    }

    assessment = await risk_svc.latest_assessment(db, project.id)
    if assessment is None:
        return {
            "assessed": False,
            "current_policy_version": risk_policy.version,
            **auth_context,
            **mode_context,
        }
    current = bool(docs) and risk_svc.content_hash(docs) == assessment.content_hash
    # Decision timeline for the drawer (risk-review-workflow spec R5.1)
    from app.models import AiRiskAssessment

    history_rows = await db.execute(
        select(AiRiskAssessment)
        .where(AiRiskAssessment.project_id == project.id)
        .order_by(AiRiskAssessment.created_at.desc())
        .limit(10)
    )
    timeline = [
        {
            "id": str(a.id),
            "score": a.score,
            "level": a.level,
            "decision": a.decision,
            "notes": a.notes,
            "assigned_group": a.assigned_group,
            "resubmission_of": str(a.resubmission_of) if a.resubmission_of else None,
            "owner_comment": a.owner_comment,
            "created_at": a.created_at.isoformat(),
            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        }
        for a in history_rows.scalars()
    ]
    # B13/B21: displayed, never scored; source tells reviewers whether key
    # protection is the platform default, explicit intent, or public opt-out.
    return {
        "assessed": True,
        "current": current,
        **auth_context,
        "id": str(assessment.id),
        "score": assessment.score,
        "level": assessment.level,
        "decision": assessment.decision,
        "status": assessment.status,
        "factors": assessment.factors,
        "notes": assessment.notes,
        "owner_comment": assessment.owner_comment,
        "created_at": assessment.created_at.isoformat(),
        "timeline": timeline,
        "policy_version": assessment.policy_version,
        "current_policy_version": risk_policy.version,
        **mode_context,
    }


@router.get("/{project_id}/activity", response_model=ActivityListOut)
async def project_activity(
    filter: str = Query("all", pattern="^(all|spec|deployments|system)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> ActivityListOut:
    """Project timeline from the audit trail (project-admin-dashboard spec R3)."""
    stmt = select(AuditLog).where(AuditLog.project_id == project.id)
    if filter == "spec":
        stmt = stmt.where(AuditLog.action.in_(SPEC_ACTIONS))
    elif filter == "deployments":
        stmt = stmt.where(AuditLog.category == "deployment")
    elif filter == "system":
        stmt = stmt.where(
            AuditLog.category.notin_(("deployment",)),
            AuditLog.action.notin_(SPEC_ACTIONS),
        )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(
        stmt.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(result.scalars())
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    emails: dict[uuid.UUID, str] = {}
    if actor_ids:
        pairs = await db.execute(select(User.id, User.email).where(User.id.in_(actor_ids)))
        emails = dict(pairs.all())
    items = [
        ActivityItemOut(
            id=r.id,
            created_at=r.created_at,
            category=r.category,
            action=r.action,
            actor_email=emails.get(r.actor_id),
            detail=r.detail or {},
        )
        for r in rows
    ]
    return ActivityListOut(items=items, total=total, page=page, page_size=page_size)
