"""Deployment endpoints (S1-08..S1-10): deploy, status, SSE logs, teardown."""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import project_owner, project_viewer
from app.api.projects import deployment_out, latest_deployment
from app.core.db import get_db
from app.models import Lease, Project
from app.schemas.projects import DeploymentOut, ExtendIn
from app.services import analytics, audit
from app.services import deployment as deploy_service
from app.services.deployment import TERMINAL_STATES, event_bus

logger = logging.getLogger("marshal.deployments")
router = APIRouter(prefix="/projects/{project_id}", tags=["deployments"])


class DeployIn(BaseModel):
    build_id: uuid.UUID | None = None  # deploy a ready codegen build (S8 R5)
    # B20 R1.1: full_governance | testbed; None → policy default_mode.
    # Ignored on in-place updates (mode is immutable custody posture).
    mode: str | None = Field(default=None, pattern="^(full_governance|testbed)$")


def _build_endpoint_auth(build) -> dict:
    """B21 deploy truth from the selected immutable build.

    New manifests carry the normalized contract. For old builds, only the
    original explicit B13 key phrase is trusted as keyed; no-signal artifacts
    were generated open under the old default and must not be reinterpreted.
    """
    if build is None:
        return {"mode": "unknown", "source": "legacy_unknown"}
    manifest_auth = ((build.manifest or {}).get("endpoint_auth") or {})
    if (
        manifest_auth.get("mode") in ("key_required", "open")
        and manifest_auth.get("source")
    ):
        return dict(manifest_auth)
    from app.services.codegen.validate import endpoint_auth_decision

    requirements = str(
        ((build.spec_snapshot or {}).get("requirements") or {}).get("content", "")
    )
    derived = endpoint_auth_decision(requirements)
    if derived["source"] == "explicit_key":
        return derived  # old B13 generated a keyed artifact for this exact signal
    if derived["source"] in ("explicit_public", "conflict"):
        return derived
    return {"mode": "unknown", "source": "legacy_unknown"}


@router.post("/deploy", response_model=DeploymentOut, status_code=202)
async def deploy(
    request: Request,
    payload: DeployIn | None = None,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> DeploymentOut:
    # Payload validation first (cheap; selecting a bad build must not trigger scoring)
    build = None
    if payload and payload.build_id:
        from app.models import CodegenBuild

        build = await db.get(CodegenBuild, payload.build_id)
        if build is None or build.project_id != project.id:
            raise HTTPException(status_code=404, detail="Build not found on this project")
        if build.status != "ready":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only a build in the READY state can be deployed. Wait for the "
                    "current build to finish, or start a new one if it failed."
                ),
            )
        preview_overlay = (build.manifest or {}).get("preview_overlay")
        if preview_overlay:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "preview_only_build",
                    "detail": (
                        "This build is a non-deployable changeset-preview overlay. "
                        "Select the accepted baseline build to deploy."
                    ),
                },
            )
        audit.set_audit_detail(
            request, build_id=str(build.id), content_hash=build.content_hash
        )

    # Platform policies (S12 R1): cheapest checks first, BEFORE the risk gate —
    # concurrency/region refusals must not spend a scoring call. In-place
    # updates replace a running stack and never add concurrency.
    from sqlalchemy import select

    from app.models import Deployment
    from app.services import policies as policies_svc

    active_mode_row = (
        await db.execute(
            select(Deployment.mode).where(
                Deployment.project_id == project.id, Deployment.status == "active"
            )
        )
    ).first()
    is_update = active_mode_row is not None
    build_auth = _build_endpoint_auth(build)
    audit.set_audit_detail(request, endpoint_auth=build_auth)

    # C1 preflight: declared connectors must be enabled + registered + active
    # BEFORE scoring/lease spend. Manifest-first (deploy truth); old builds
    # predate the key ⇒ empty list, nothing to check.
    declared_connectors = [
        str(s)
        for s in (((build.manifest or {}) if build else {}).get("declared_connectors") or [])
    ]
    if declared_connectors:
        from app.services.platform_settings import get_controls

        controls = await get_controls()
        if not controls.feature_flags.get("connectors_enabled", False):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "connectors_disabled",
                    "detail": (
                        "This build declares connectors, but connector "
                        "consumption is not enabled on this installation"
                    ),
                },
            )
        from app.models import PlatformSettings

        settings_row = await db.get(PlatformSettings, 1)
        registry = {
            e.get("slug"): e for e in ((settings_row.connectors if settings_row else None) or [])
        }
        unavailable = [
            slug
            for slug in declared_connectors
            if slug not in registry or not registry[slug].get("active", True)
        ]
        if unavailable:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "connector_unavailable",
                    "detail": (
                        "Declared connector(s) not registered and active: "
                        f"{', '.join(unavailable)} — register/reactivate in "
                        "Admin → Integrations, or rebuild without the declaration"
                    ),
                },
            )
        # C3 v1: MCP tool sources must still be mcp_server-typed at deploy
        # time (the build checked too, but the registry can change between).
        mcp_declared = [
            str(s)
            for s in (((build.manifest or {}) if build else {}).get("mcp_tool_connectors") or [])
        ]
        wrong_type = [
            slug
            for slug in mcp_declared
            if registry.get(slug, {}).get("type") != "mcp_server"
        ]
        if wrong_type:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "connector_unavailable",
                    "detail": (
                        "MCP tool connector(s) are no longer mcp_server-typed: "
                        f"{', '.join(wrong_type)} — fix the registry entry or "
                        "rebuild without the MCP declaration"
                    ),
                },
            )
        audit.set_audit_detail(
            request,
            declared_connectors=declared_connectors,
            mcp_tool_connectors=mcp_declared or None,
        )

    # Agent substance R3.2: the CURRENT template rail is re-checked against
    # the BUILD manifest's declared capabilities — a rail tightened after the
    # build refuses the deploy by name (never a stale-build bypass).
    if build and build.manifest and project.template_id:
        from app.models import Template as TemplateModel
        from app.services.capabilities import blocked_for_manifest

        template_row = await db.get(TemplateModel, project.template_id)
        blocked = blocked_for_manifest(build.manifest or {}, template_row)
        if blocked:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "capability_not_allowed",
                    "detail": (
                        f"Template '{template_row.name if template_row else 'unknown'}' "
                        "does not allow the built capabilit"
                        f"{'y' if len(blocked) == 1 else 'ies'}: {', '.join(blocked)} "
                        "— adjust the template rail or rebuild without the declaration"
                    ),
                },
            )

    # Composable agents R1.4/R4.2: every declared dependency must exist, be
    # visible to the deployer under existing access rules, and hold an ACTIVE
    # deployment (a gate-blocked agent cannot BE active — the callable
    # endpoint is the approved artifact). Named refusals BEFORE scoring/lease.
    dependencies = (project.composition or {}).get("dependencies") or []
    if dependencies:
        from app.services import collab

        problems: list[str] = []
        for dep in dependencies:
            slug = str(dep.get("slug"))
            try:
                dep_id = uuid.UUID(str(dep.get("project_id")))
            except (ValueError, TypeError):
                problems.append(f"{slug} (unresolvable)")
                continue
            resolved = await collab.resolve_role(db, dep_id, request.state.user)
            if resolved is None:
                problems.append(f"{slug} (not found or no access)")
                continue
            dep_active = (
                await db.execute(
                    select(Deployment).where(
                        Deployment.project_id == dep_id,
                        Deployment.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if dep_active is None or not dep_active.app_url:
                problems.append(f"{slug} (no active deployment)")
        if problems:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "composition_dependency_unavailable",
                    "detail": (
                        "Declared agent dependencies are not callable: "
                        + ", ".join(problems)
                        + " — deploy the dependencies first"
                    ),
                },
            )
        audit.set_audit_detail(
            request, composition_dependencies=[str(d.get("slug")) for d in dependencies]
        )
    try:
        deploy_policies = await policies_svc.enforce_deploy_preflight(
            db,
            request.state.user.id,
            is_update=is_update,
            endpoint_auth=build_auth,
        )
    except policies_svc.PolicyViolation as exc:
        status = 422 if exc.code in ("policy_region", "policy_endpoint_auth") else 409
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "detail": str(exc)}
        ) from exc

    # B20 R1.1: resolve the deployment MODE — custody posture picks the gate
    # stance. Updates INHERIT the active row's mode (immutable; the payload
    # field is ignored); creates take the payload or the policy preselect,
    # and testbed creates are policy-gated.
    if is_update:
        effective_mode = active_mode_row[0] or "full_governance"
    else:
        effective_mode = (
            payload.mode if payload and payload.mode else None
        ) or deploy_policies.default_mode
        if effective_mode == "testbed" and not deploy_policies.testbed_enabled:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "policy_testbed_disabled",
                    "detail": "Testbed deployments are disabled by platform policy",
                },
            )
    gate_mode = (
        "enforce" if effective_mode == "full_governance"
        else deploy_policies.testbed_gate_mode
    )
    audit.set_audit_detail(request, mode=effective_mode, gate_mode=gate_mode)

    # Risk gate (S4-03): current spec content must be approved before leasing
    # starts. Scores synchronously on a hash miss (one fast-model call).
    # B20 R1.3: full_governance ENFORCES (unchanged, fixed — the name is the
    # contract); testbed runs the SAME scoring pipeline in ADVISORY (scored,
    # stored, routed for review, displayed — never blocking) or skips scoring
    # entirely under OFF.
    from app.services import risk as risk_svc

    if gate_mode == "enforce":
        try:
            await risk_svc.deployment_gate(db, project, request.state.user)
        except risk_svc.ScoringInProgress as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "risk_scoring", "detail": "Risk scoring in progress — retry shortly"},
            ) from exc
        except risk_svc.GateBlocked as exc:
            raise HTTPException(status_code=403, detail={"code": exc.code, **exc.payload}) from exc
    elif gate_mode == "advisory":
        try:
            await risk_svc.deployment_gate(db, project, request.state.user)
        except (risk_svc.ScoringInProgress, risk_svc.GateBlocked) as exc:
            # The verdict is recorded on the assessment and the bypass leaves
            # audit evidence — the deploy proceeds (testbed custody).
            audit.set_audit_detail(
                request, gate="advisory_bypassed",
                gate_code=getattr(exc, "code", "risk_scoring"),
            )
    # gate_mode == "off": no scoring at deploy (R1.3)

    try:
        deployment = await deploy_service.start_deployment(
            db, request.state.user, project, build=build, mode=effective_mode
        )
    except policies_svc.PolicyViolation as exc:
        # Authoritative post-risk capacity recheck: a race loser receives the
        # same structured policy response as the cheap preflight.
        status = 422 if exc.code in ("policy_region", "policy_endpoint_auth") else 409
        raise HTTPException(
            status_code=status, detail={"code": exc.code, "detail": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    analytics.track(request.state.user.id, "deploy_started", project_id=project.id)  # S16-04
    return (await deployment_out(db, deployment))  # type: ignore[return-value]


@router.post("/deployment/api-key/reveal")
async def reveal_deployment_api_key(
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """B13 owner reveal (integration-wave R1.4): the key value is fetched LIVE
    from the Enclave and never stored platform-side. Audited as a SECURITY
    event (deployment_api_key_revealed) — reading a credential is an action."""
    from app.services import audit

    try:
        result = await deploy_service.fetch_live_api_key(db, project)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, deployment_id=result["deployment_id"], api_key_id=result["api_key_id"]
    )
    return result


@router.post("/deployment/testbed-credentials")
async def issue_testbed_credentials(
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """B20 R2.3: mint limited, time-boxed credentials for a Testbed
    deployment's leased account. Nothing is stored platform-side; audited as
    SECURITY (testbed_credentials_issued) — issuing a credential is an
    action, the B13 reveal precedent."""
    from app.services import testbed as testbed_svc

    try:
        result = await testbed_svc.issue_credentials(db, project, request.state.user)
    except testbed_svc.TestbedDisabled as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "policy_testbed_disabled", "detail": str(exc)},
        ) from exc
    except testbed_svc.TestbedUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "testbed_unavailable", "detail": str(exc)},
        ) from exc
    audit.set_audit_detail(
        request,
        deployment_id=result["deployment_id"],
        account_id=result["account_id"],
        session_name=result["session_name"],
        expires_at=result["expires_at"],
    )
    return result


@router.post("/deployment/preview")
async def preview_deployment_changes(
    request: Request,
    payload: DeployIn | None = None,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Changeset preview for an in-place redeploy: what WOULD change, before
    committing. Creates + deletes a CloudFormation change set; the running
    stack is never modified. Same build-selection semantics as POST /deploy."""
    build = None
    if payload and payload.build_id:
        from app.models import CodegenBuild

        build = await db.get(CodegenBuild, payload.build_id)
        if build is None or build.project_id != project.id:
            raise HTTPException(status_code=404, detail="Build not found on this project")
        if build.status != "ready":
            raise HTTPException(
                status_code=409,
                detail="Only a build in the READY state can be previewed.",
            )
        audit.set_audit_detail(request, build_id=str(build.id))
    try:
        result = await deploy_service.preview_update(db, project, build)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.set_audit_detail(
        request,
        no_changes=result["no_changes"],
        change_count=len(result["changes"]),
    )
    return result


@router.get("/deployment", response_model=DeploymentOut)
async def get_deployment(
    project: Project = Depends(project_viewer), db: AsyncSession = Depends(get_db)
) -> DeploymentOut:
    deployment = await latest_deployment(db, project.id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="No deployment for this project")
    return (await deployment_out(db, deployment, with_lease_state=True))  # type: ignore[return-value]


@router.get("/deployments", response_model=list[DeploymentOut])
async def deployment_history(
    project: Project = Depends(project_viewer), db: AsyncSession = Depends(get_db)
) -> list[DeploymentOut]:
    """Attempt history (S11 R4) — every row, provenance included."""
    from sqlalchemy import select

    from app.models import Deployment, User

    rows = list(
        (
            await db.execute(
                select(Deployment)
                .where(Deployment.project_id == project.id)
                .order_by(Deployment.created_at.desc())
                .limit(30)
            )
        ).scalars()
    )
    names: dict = {}
    ids = {r.user_id for r in rows}
    if ids:
        users = await db.execute(select(User).where(User.id.in_(ids)))
        names = {u.id: (u.name or u.email.split("@")[0]) for u in users.scalars()}
    out = []
    for row in rows:
        item = await deployment_out(db, row)
        item.created_by_name = names.get(row.user_id)
        out.append(item)
    return out


@router.post("/deployment/extend", response_model=DeploymentOut)
async def extend_deployment(
    payload: ExtendIn,
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> DeploymentOut:
    """Owner-only TTL extension within policy + lease clamp (S11 R5.3/R6.3)."""
    from datetime import UTC, datetime, timedelta

    deployment = await latest_deployment(db, project.id)
    if deployment is None or deployment.status != "active":
        raise HTTPException(
            status_code=409,
            detail=(
                "Only a live deployment can be extended. If it already expired, "
                "deploy the ready build again."
            ),
        )
    _default, max_ttl = await deploy_service.resolve_ttl_hours(db)
    now = datetime.now(UTC)
    base = deployment.expires_at
    if base is not None and base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    proposed = (base or now) + timedelta(hours=payload.hours)
    activated = deployment.deployed_at
    if activated is not None and activated.tzinfo is None:
        activated = activated.replace(tzinfo=UTC)
    ceiling = (activated or now) + timedelta(hours=max_ttl)
    if proposed > ceiling:
        raise HTTPException(
            status_code=422,
            detail=f"Extension exceeds the {max_ttl}h maximum TTL (policy)",
        )
    # Lease clamp: platform teardown must precede ISB reaping (R6.3)
    if deployment.lease_id:
        lease = await db.get(Lease, deployment.lease_id)
        if lease and lease.external_lease_id:
            # B20 R0.3: ask the provider that OWNS this lease
            state = await deploy_service.provider_for_lease(lease).lease_state(
                lease.external_lease_id
            )
            lease_expiry = (state or {}).get("expires_at")
            if lease_expiry:
                from datetime import datetime as dt

                try:
                    lease_dt = dt.fromisoformat(str(lease_expiry).replace("Z", "+00:00"))
                    clamp = lease_dt - timedelta(hours=deploy_service.LEASE_CLAMP_HOURS)
                    if proposed > clamp:
                        raise HTTPException(
                            status_code=422,
                            detail="Extension is limited by the Enclave lease expiry",
                        )
                except ValueError:
                    pass  # unparseable lease expiry → skip the clamp (best-effort)
    before = deployment.expires_at.isoformat() if deployment.expires_at else None
    deployment.expires_at = proposed
    await db.commit()
    await db.refresh(deployment)
    audit.set_audit_detail(
        request, before={"expires_at": before}, after={"expires_at": proposed.isoformat()},
        hours=payload.hours,
    )
    return (await deployment_out(db, deployment, with_lease_state=True))  # type: ignore[return-value]


@router.get("/deployment/logs")
async def stream_deployment_logs(
    project: Project = Depends(project_viewer), db: AsyncSession = Depends(get_db)
):
    deployment = await latest_deployment(db, project.id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="No deployment for this project")
    deployment_id = str(deployment.id)
    is_terminal = deployment.status in TERMINAL_STATES

    async def event_stream():
        # Replay persisted timeline first so late subscribers see full history
        for entry in deployment.timeline or []:
            yield {"event": "phase", "data": json.dumps(entry)}
        if is_terminal:
            yield {"event": "done", "data": json.dumps({"status": deployment.status})}
            return
        queue = await event_bus.subscribe(deployment_id)
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "done":
                    yield {"event": "done", "data": json.dumps(event)}
                    return
                yield {"event": event.get("type", "cfn"), "data": json.dumps(event)}
        finally:
            await event_bus.unsubscribe(deployment_id, queue)

    return EventSourceResponse(event_stream())


@router.post("/deployment/teardown", response_model=DeploymentOut, status_code=202)
async def teardown(
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> DeploymentOut:
    # Tear down what is RUNNING. A failed in-place update leaves the prior
    # deployment active by design (S11 rollback), so the LATEST row can be a
    # failed attempt sharing that stack — tearing that down while the active
    # row survives stranded the project with an "active" record whose stack
    # was gone (observed live 27 Aug). Prefer the active row; fall back to
    # latest so failed-first-deploy cleanup still works.
    from sqlalchemy import select as _select

    from app.models import Deployment as _Deployment

    deployment = (
        await db.execute(
            _select(_Deployment).where(
                _Deployment.project_id == project.id, _Deployment.status == "active"
            )
        )
    ).scalar_one_or_none() or await latest_deployment(db, project.id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="No deployment for this project")
    try:
        await deploy_service.start_teardown(db, deployment)
    except deploy_service.DependentsBlockTeardown as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "composition_dependents",
                "detail": str(exc),
                "dependents": exc.dependents,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    analytics.track(request.state.user.id, "teardown", project_id=project.id)  # S16-04
    return (await deployment_out(db, deployment))  # type: ignore[return-value]
