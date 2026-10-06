"""Build job pipeline (codegen-handoff spec R2 + external-codegen spec R4).

Two lifecycles behind one seam:
  internal (in-process): queued → generating → validating → ready|failed|cancelled
  external (workspace):  queued → dispatched → generating → validating → …

The CodegenBuild row is the durable state, an in-memory bus feeds SSE, and a
startup rehydrator resolves jobs orphaned by restarts: in-process builds FAIL
loudly (S2 pattern — the work died with the process); external builds
RE-ATTACH (the work continues in CodeBuild; we just resume polling).
"""

import asyncio
import dataclasses
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    CodegenArtifact,
    CodegenBuild,
    PlatformSettings,
    Project,
    Spec,
    Template,
    User,
)
from app.services.codegen import contract
from app.services.codegen.conformance import conformance_report
from app.services.codegen.internal import SizeEscalationDependencyError
from app.services.codegen.literals import (
    contracts_from_payload,
    contracts_payload,
    extract_contracts,
)
from app.services.codegen.provider import BuildCtx, get_provider, resolve_provider_name
from app.services.codegen.validate import (
    MAX_INLINE_CHARS,
    Finding,
    declared_connectors,
    declared_tools,
    endpoint_auth_decision,
    mcp_tool_connectors,
    requires_memory,
    requires_packaged,
    requires_planning,
    requires_web_console,
    validate_artifacts,
)
from app.services.deployment import DeploymentEventBus
from app.services.guardrails import resolve_models
from app.services.ratelimit import RateLimited
from app.services.spend import CostCapExceeded

logger = logging.getLogger("marshal.codegen")

codegen_bus = DeploymentEventBus()
_tasks: dict[str, asyncio.Task] = {}
_cancel_flags: set[str] = set()

ACTIVE_STATES = ("queued", "dispatched", "generating", "validating")
TERMINAL_STATES = ("ready", "failed", "cancelled")
# In-process builds. 480 → 720 (13.5AA): a size-escalated build runs TWO
# generation passes (~3 min each live) plus the CodeBuild packaging pass
# (~90 s) — 8 min was a coin flip. This is a hung-call safety net, not a
# budget: per-call read timeouts and retry caps bound the model seam.
BUILD_TIMEOUT_S = 720
POLL_INTERVAL_S = 15  # external builds
DOC_TYPES = ("requirements", "design", "tasks")


def _now() -> datetime:
    return datetime.now(UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # sqlite returns naive datetimes
        return dt.replace(tzinfo=UTC)
    return dt


class BuildInProgress(Exception):
    """One active build per project (R2.1) — surfaces as 409."""


class NothingToBuild(Exception):
    """Project lacks spec documents — surfaces as 409."""


async def _publish(build_id: uuid.UUID, event: dict) -> None:
    await codegen_bus.publish(str(build_id), event)


async def _set_phase(
    db: AsyncSession, build: CodegenBuild, status: str, detail: str | None = None
) -> None:
    build.status = status
    build.phase_detail = detail
    await db.commit()
    await _publish(build.id, {"type": "phase", "phase": status, "detail": detail})


def _spawn(build_id: uuid.UUID, coro) -> None:
    task = asyncio.create_task(coro)
    _tasks[str(build_id)] = task
    task.add_done_callback(lambda t: _tasks.pop(str(build_id), None))


# ------------------------------------------------------------------ start


class ProfileUnavailable(Exception):
    """Profile/provider mismatch (cdk-artifacts R1.4) — surfaces as 409."""


async def _resolve_profile(
    db: AsyncSession, project: Project, requested: str | None, requirements: str
) -> str:
    # Agent substance R1.2: the spec signal is the ONLY door into the
    # packaged profile, and a declared spec never silently builds another
    # profile — conflicts refuse loudly, never resolve quietly.
    if requires_packaged(requirements):
        if requested not in (None, "packaged-cfn"):
            raise ProfileUnavailable(
                "Spec declares packaged dependencies — the "
                f"'{requested}' profile cannot override the declaration"
            )
        return "packaged-cfn"
    if requested == "packaged-cfn":
        raise ProfileUnavailable(
            "The packaged profile is spec-declared — add 'SHALL use packaged "
            "dependencies' to the requirements document"
        )
    if requested is not None:
        if requested not in contract.ARTIFACT_PROFILES:
            raise ProfileUnavailable(f"Unknown artifact profile '{requested}'")
        return requested
    if project.template_id:
        template = await db.get(Template, project.template_id)
        default = ((template.scaffolding or {}).get("codegen") or {}).get("artifact_profile")
        # packaged-cfn is deliberately excluded: template defaults cannot
        # select it (R1.2 — never chosen silently).
        if default in ("inline-cfn", "cdk-app"):
            return default
    return "inline-cfn"


async def start_build(
    db: AsyncSession, user: User, project: Project, *, artifact_profile: str | None = None
) -> CodegenBuild:
    active = (
        await db.execute(
            select(CodegenBuild).where(
                CodegenBuild.project_id == project.id,
                CodegenBuild.status.in_(ACTIVE_STATES),
            )
        )
    ).scalar_one_or_none()
    if active is not None:
        raise BuildInProgress("A build is already running for this project")

    snapshot: dict[str, dict] = {}
    for doc_type in DOC_TYPES:
        spec = (
            await db.execute(
                select(Spec)
                .where(Spec.project_id == project.id, Spec.type == doc_type)
                .order_by(Spec.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if spec:
            snapshot[doc_type] = {"version": spec.version, "content": spec.content}
    if "requirements" not in snapshot or "design" not in snapshot:
        raise NothingToBuild(
            "Builds need at least requirements and design documents — generate or save them first"
        )

    provider_name = await resolve_provider_name()
    get_provider(provider_name)  # constructability check → 503 before a row exists
    profile = await _resolve_profile(
        db, project, artifact_profile,
        str((snapshot.get("requirements") or {}).get("content", "")),
    )
    if profile == "cdk-app" and provider_name == "internal":
        raise ProfileUnavailable(
            "The cdk-app profile needs the workspace runner (synth toolchain lives "
            "there) — an admin can switch the codegen provider under Model Controls."
        )
    if profile == "packaged-cfn":
        # Agent substance R1: generation stays in-process; the packaging pass
        # runs on the S9 CodeBuild seam — pip never runs on the control plane.
        if provider_name != "internal":
            raise ProfileUnavailable(
                "The packaged profile v1 runs on the internal provider (the "
                "packaging pass uses the workspace runner separately)"
            )
        if not get_settings().codegen_workspace_bucket:
            raise ProfileUnavailable(
                "The packaged profile needs the workspace runner infrastructure "
                "(CODEGEN_WORKSPACE_BUCKET is unset)"
            )

    spec_hash = _sha(
        "\n".join(f"{t}:{snapshot[t]['content']}" for t in DOC_TYPES if t in snapshot)
    )
    build = CodegenBuild(
        project_id=project.id,
        status="queued",
        provider=provider_name,
        artifact_profile=profile,
        spec_snapshot=snapshot,
        spec_hash=spec_hash,
        created_by=user.id,
    )
    db.add(build)
    await db.commit()
    await db.refresh(build)

    if provider_name == "internal":
        _spawn(build.id, _run_build(build.id))
    else:
        _spawn(build.id, _run_external(build.id))
    return build


def request_cancel(build_id: uuid.UUID) -> None:
    """In-process builds: cooperative flag checked between files."""
    _cancel_flags.add(str(build_id))


def _cancelled(build_id: uuid.UUID) -> bool:
    return str(build_id) in _cancel_flags


async def cancel_external(db: AsyncSession, build: CodegenBuild) -> None:
    """External builds: tombstone + StopBuild, terminal immediately (R4.5).

    The poll task observes the terminal status and exits; a late-writing
    engine's results are ignored because ingest re-checks status.
    """
    provider = get_provider(build.provider)
    attempt = 2 if build.retried else 1
    await provider.cancel(build.id, attempt, build.external_job_id)
    build.status = "cancelled"
    build.phase_detail = None
    build.finished_at = _now()
    await db.commit()
    await _publish(build.id, {"type": "done", "status": "cancelled"})


# ------------------------------------------------------------------ shared


async def _fail(
    db: AsyncSession, build: CodegenBuild, code: str, message: str, findings: list | None = None
) -> None:
    build.status = "failed"
    build.error = {"code": code, "message": message[:1000], "findings": findings or []}
    build.finished_at = _now()
    await db.commit()
    await _publish(build.id, {"type": "done", "status": "failed", "error": build.error})
    if build.created_by:
        from app.services import notifications as notif

        notif.emit(
            notif.emit_for_user(
                build.created_by,
                type="build_failed",
                title="Agent build failed",
                body=message[:200],
                link=f"/projects/{build.project_id}?tab=build",
                dedupe_key=f"build_failed:{build.id}",
            )
        )


async def _build_inputs(db: AsyncSession, build: CodegenBuild) -> tuple[BuildCtx, list, str]:
    """Resolve BuildCtx + template forbidden patterns + project slug."""
    from app.services.export import project_slug

    project = await db.get(Project, build.project_id)
    template = await db.get(Template, project.template_id) if project.template_id else None
    models = await resolve_models(template)
    forbidden = list(
        ((template.guardrails or {}).get("architecture", {}) or {}).get(
            "forbidden_patterns", []
        )
        if template
        else []
    )
    requirements = _snapshot_requirements(build)
    auth = endpoint_auth_decision(requirements)
    literal_contract = contracts_payload(extract_contracts(requirements))
    declared, mcp_declared = await _spec_connector_sets(build)
    # Composable agents: parse-only from the frozen snapshot (slugs; the
    # resolved graph lives on the project row via the save-seam sync).
    from app.services.composition import declared_dependencies

    agent_deps, orchestrated = declared_dependencies(requirements)
    if mcp_declared:
        # C3 v1: fail FAST at build start when the declaration cannot work —
        # the deploy preflight re-checks (registry can change in between).
        if len(mcp_declared) > 1:
            raise ValueError(
                "v1 supports exactly one MCP tool connector per agent — "
                f"declared: {', '.join(mcp_declared)}"
            )
        settings_row = await db.get(PlatformSettings, 1)
        registry = {
            e.get("slug"): e
            for e in ((settings_row.connectors if settings_row else None) or [])
        }
        for slug in mcp_declared:
            entry = registry.get(slug)
            if entry is None or not entry.get("active", True):
                raise ValueError(
                    f"MCP connector '{slug}' is not registered and active — "
                    "register it in Admin → Integrations first"
                )
            if entry.get("type") != "mcp_server":
                raise ValueError(
                    f"Connector '{slug}' is type '{entry.get('type')}' — MCP "
                    "tools require an mcp_server connector"
                )
    ctx = BuildCtx(
        build_id=build.id,
        project_id=build.project_id,
        project_name=project.name,
        user_id=build.created_by,
        spec_docs={t: d["content"] for t, d in (build.spec_snapshot or {}).items()},
        # S17-05 purpose pin: codegen resolves through the Bedrock-only field —
        # generated-code provenance stays on AWS regardless of the allowlist.
        model_id=models.codegen,
        max_tokens=models.max_tokens_generation,
        guardrail_rules=[f"avoid: {p}" for p in forbidden],
        endpoint_auth=auth,
        require_api_key=auth["mode"] == "key_required",
        require_web_console=_spec_requires_console(build),
        literal_contract=literal_contract,
        declared_connectors=declared,
        mcp_tool_connectors=mcp_declared,
        agent_dependencies=agent_deps,
        orchestrated=orchestrated,
        require_memory=requires_memory(requirements),
        require_packaged=requires_packaged(requirements),
        declared_tools=declared_tools(requirements),
        require_planning=requires_planning(requirements),
        loop_iteration_cap=_loop_cap(template),
    )
    return ctx, forbidden, project_slug(project)


def _loop_cap(template: Template | None) -> int:
    """Template-rail loop-iteration ceiling (agent-substance R3.2): guardrails
    .capabilities.max_loop_iterations, clamped to 1..8, default 5."""
    raw = (
        ((template.guardrails or {}).get("capabilities", {}) or {}).get(
            "max_loop_iterations"
        )
        if template
        else None
    )
    try:
        return max(1, min(int(raw), 8)) if raw is not None else 5
    except (TypeError, ValueError):
        return 5


def _snapshot_requirements(build: CodegenBuild) -> str:
    return str(
        ((build.spec_snapshot or {}).get("requirements") or {}).get("content", "")
    )


def _spec_endpoint_auth(build: CodegenBuild) -> dict:
    """B21 posture from the build's immutable requirements snapshot."""
    return endpoint_auth_decision(_snapshot_requirements(build))


def _spec_requires_key(build: CodegenBuild) -> bool:
    """Compatibility helper for compaction/provider code."""
    return _spec_endpoint_auth(build)["mode"] == "key_required"


def _spec_requires_console(build: CodegenBuild) -> bool:
    """B19 console signal — same frozen-snapshot discipline as B13."""
    return requires_web_console(_snapshot_requirements(build))


async def _spec_connector_sets(build: CodegenBuild) -> tuple[list[str], list[str]]:
    """(declared_union, mcp_tool_subset) — frozen-snapshot signals, FLAG-GATED.

    connectors_enabled off ⇒ both empty: the phrases are inert prose (the C0
    honesty contract). The union rides C1 custody (secret copy, preflight,
    IAM) so MCP tool connectors need no second custody surface. The gate
    re-derives through this same helper, so a mid-build flag flip converges
    on the stricter read at validation time.
    """
    from app.services.platform_settings import get_controls

    controls = await get_controls()
    if not controls.feature_flags.get("connectors_enabled", False):
        return [], []
    requirements = _snapshot_requirements(build)
    mcp = mcp_tool_connectors(requirements)
    union = sorted(set(declared_connectors(requirements)) | set(mcp))[:5]
    return union, mcp


async def _spec_declared_connectors(build: CodegenBuild) -> list[str]:
    """C1 compatibility wrapper — the union list (custody truth)."""
    union, _mcp = await _spec_connector_sets(build)
    return union


async def _finalize(
    db: AsyncSession,
    build: CodegenBuild,
    *,
    generated: dict[str, str],
    forbidden: list,
    model_id: str,
    app_name: str,
    architecture_notes: str,
    endpoint_auth: dict,
    literal_contract: dict,
    manifest_extra: dict | None = None,
) -> None:
    """Structural/auth gate → conformance gate → ready. Shared by every
    provider and artifact profile; providers can improve outputs but cannot
    weaken platform truth."""
    await _set_phase(db, build, "validating", "Running the validation gate")
    declared, mcp_declared = await _spec_connector_sets(build)  # same flag-gated read
    from app.services.composition import declared_dependencies

    agent_deps, orchestrated = declared_dependencies(_snapshot_requirements(build))
    requirements_snapshot = _snapshot_requirements(build)
    project = await db.get(Project, build.project_id)
    template = (
        await db.get(Template, project.template_id)
        if project and project.template_id
        else None
    )
    tools_list = declared_tools(requirements_snapshot)
    planning = requires_planning(requirements_snapshot)
    # 13.5AA: on a size-escalated build the PLATFORM is the packaged declarer
    # (empty manifest, no dependencies) — the symmetry gate must not demand
    # the spec phrase, whose job is opting into third-party code.
    size_escalated = bool((manifest_extra or {}).get("size_escalation"))
    packaged_truth = requires_packaged(requirements_snapshot) or size_escalated
    findings, unenforceable = validate_artifacts(
        generated,
        forbidden,
        profile=build.artifact_profile,
        endpoint_auth=endpoint_auth,
        require_web_console=_spec_requires_console(build),
        declared_connectors=declared,
        mcp_tool_connectors=mcp_declared,
        agent_dependencies=agent_deps,
        orchestrated=orchestrated,
        require_memory=requires_memory(requirements_snapshot),
        require_packaged=packaged_truth,
        declared_tools=tools_list,
        require_planning=planning,
        loop_iteration_cap=_loop_cap(template),
    )
    if size_escalated and generated.get("requirements.txt", "").strip():
        # Belt and braces: the provider refuses third-party imports on an
        # escalated build; a non-empty manifest here means that seam was
        # bypassed (another provider) — the gate is platform truth.
        findings.append(
            Finding(
                "packaged_dependencies", "requirements.txt",
                "size-escalated build must ship an EMPTY dependency manifest — "
                "declare 'SHALL use packaged dependencies' to allow libraries",
            )
        )
    manifest = {
        "provider": build.provider,
        "model_id": model_id,
        "app_name": app_name,
        "architecture_notes": architecture_notes,
        "endpoint_auth": endpoint_auth,
        "declared_connectors": declared,
        "mcp_tool_connectors": mcp_declared,
        "agent_dependencies": agent_deps,
        "orchestrated": orchestrated,
        "memory": requires_memory(requirements_snapshot),
        # True for spec-declared AND size-escalated builds: the deploy-preflight
        # rail check and the deployer read this as "ships as an S3 asset".
        "packaged": packaged_truth,
        "tools": tools_list,
        "planning": planning,
        "literal_contract": literal_contract,
        "files": [
            {"path": p, "sha256": _sha(c), "bytes": len(c.encode())}
            for p, c in sorted(generated.items())
        ],
        "spec_hash": build.spec_hash,
        "validation": {
            "findings": [f.as_dict() for f in findings],
            "unenforceable_patterns": unenforceable,
        },
        "timings": {
            "started_at": build.started_at.isoformat() if build.started_at else None,
            "finished_at": _now().isoformat(),
        },
        **(manifest_extra or {}),
    }
    build.content_hash = _sha(
        "\n".join(f"{p}:{_sha(c)}" for p, c in sorted(generated.items()))
    )
    if findings:
        build.manifest = manifest
        await _fail(
            db, build, "validation_failed",
            f"{len(findings)} validation finding(s) — fix the spec and rebuild",
            findings=[f.as_dict() for f in findings],
        )
        return

    await _set_phase(db, build, "validating", "Reviewing spec conformance")
    try:
        report = await conformance_report(
            db,
            build,
            generated,
            contracts=contracts_from_payload(literal_contract),
        )
    except Exception:  # noqa: BLE001 — belt over the service's own braces
        logger.exception("Conformance review crashed for build %s", build.id)
        report = {"status": "unavailable", "error": "conformance review crashed"}
    manifest["conformance"] = report

    # B23: ONLY deterministic missing field/literal checks block. The model
    # review remains visible but advisory because it can be wrong.
    conformance_findings = [
        Finding(
            str(v.get("check") or "spec_conformance"),
            "requirements.md",
            f"{v.get('evidence', 'Generated source violates the literal contract')}. "
            f"Criterion: {v.get('criterion', '')}",
        )
        for v in (report.get("verdicts") or [])
        if v.get("source") == "deterministic" and v.get("verdict") == "violated"
    ]
    if conformance_findings:
        serialized = [f.as_dict() for f in conformance_findings]
        manifest["validation"]["findings"].extend(serialized)
        build.manifest = manifest
        await _fail(
            db,
            build,
            "validation_failed",
            f"{len(serialized)} deterministic spec-conformance finding(s) — "
            "generated code did not preserve the approved field/literal contract",
            findings=serialized,
        )
        return

    if manifest.get("size_escalation"):
        manifest["size_escalation"] = {**manifest["size_escalation"], "resolved": True}
    build.manifest = manifest
    build.status = "ready"
    build.phase_detail = None
    build.finished_at = _now()
    await db.commit()
    await _publish(build.id, {"type": "done", "status": "ready", "content_hash": build.content_hash})

    if build.created_by:
        from app.services import notifications as notif

        notif.emit(
            notif.emit_for_user(
                build.created_by,
                type="build_ready",
                title="Your agent build is ready",
                body=f'"{app_name}" was generated and passed validation — deploy it from the Build tab.',
                link=f"/projects/{build.project_id}?tab=build",
                dedupe_key=f"build_ready:{build.id}",
            )
        )


_PREVIEW_OVERLAY_MARKER = "## Rehearsal-only SNS candidate (DO NOT DEPLOY)"
_PREVIEW_TOPIC_LOGICAL_ID = "ApprovalDecisionTopic"
_PREVIEW_TOPIC_TYPE = "AWS::SNS::Topic"


def _snapshot_content(build: CodegenBuild, doc_type: str) -> str:
    return str(((build.spec_snapshot or {}).get(doc_type) or {}).get("content", ""))


async def _try_preview_overlay(
    db: AsyncSession,
    build: CodegenBuild,
    ctx: BuildCtx,
    forbidden: list,
) -> bool:
    """Derive an explicit preview-only SNS candidate from its accepted baseline.

    The marker is appended to the canonical design by the rehearsal. Reusing the
    accepted source/template makes the CloudFormation change set exactly one topic
    instead of allowing unrelated model regeneration drift into a non-deployed
    preview artifact.
    """
    if build.provider != "internal" or build.artifact_profile != "inline-cfn":
        return False
    candidate_design = _snapshot_content(build, "design")
    if (
        _PREVIEW_OVERLAY_MARKER not in candidate_design
        or _PREVIEW_TOPIC_LOGICAL_ID not in candidate_design
        or _PREVIEW_TOPIC_TYPE not in candidate_design
    ):
        return False
    baseline_design = candidate_design.split(_PREVIEW_OVERLAY_MARKER, 1)[0].rstrip()
    candidates = list(
        (
            await db.execute(
                select(CodegenBuild)
                .where(
                    CodegenBuild.project_id == build.project_id,
                    CodegenBuild.id != build.id,
                    CodegenBuild.status == "ready",
                    CodegenBuild.provider == "internal",
                    CodegenBuild.artifact_profile == "inline-cfn",
                )
                .order_by(CodegenBuild.created_at.desc())
                .limit(30)
            )
        ).scalars()
    )
    baseline = next(
        (
            item
            for item in candidates
            if _snapshot_content(item, "requirements")
            == _snapshot_content(build, "requirements")
            and _snapshot_content(item, "tasks") == _snapshot_content(build, "tasks")
            and _snapshot_content(item, "design").rstrip() == baseline_design
        ),
        None,
    )
    if baseline is None:
        return False

    rows = list(
        (
            await db.execute(
                select(CodegenArtifact)
                .where(CodegenArtifact.build_id == baseline.id)
                .order_by(CodegenArtifact.path)
            )
        ).scalars()
    )
    if not rows or any(row.content is None for row in rows):
        return False
    generated = {row.path: str(row.content) for row in rows}
    template = generated.get("template.json")
    if template is None:
        return False
    try:
        document = json.loads(template)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Accepted baseline template is invalid JSON: {exc}") from exc
    resources = document.get("Resources") if isinstance(document, dict) else None
    if not isinstance(resources, dict):
        raise ValueError("Accepted baseline template has no Resources object")
    topics = [
        logical_id
        for logical_id, resource in resources.items()
        if isinstance(resource, dict) and resource.get("Type") == _PREVIEW_TOPIC_TYPE
    ]
    if topics:
        raise ValueError("Canonical baseline unexpectedly contains an SNS topic")
    resources[_PREVIEW_TOPIC_LOGICAL_ID] = {"Type": _PREVIEW_TOPIC_TYPE}
    generated["template.json"] = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
    )

    await _set_phase(db, build, "generating", "Deriving preview overlay from accepted baseline")
    for row in rows:
        content = generated[row.path]
        db.add(
            CodegenArtifact(
                build_id=build.id,
                path=row.path,
                content=content,
                content_hash=_sha(content),
                size_bytes=len(content.encode()),
                language=row.language,
            )
        )
    await db.commit()
    baseline_manifest = baseline.manifest or {}
    await _finalize(
        db,
        build,
        generated=generated,
        forbidden=forbidden,
        model_id=ctx.model_id,
        app_name=str(baseline_manifest.get("app_name") or ctx.project_name),
        architecture_notes=str(baseline_manifest.get("architecture_notes") or ""),
        endpoint_auth=ctx.endpoint_auth,
        literal_contract=ctx.literal_contract,
        manifest_extra={
            "preview_overlay": {
                "kind": "sns_topic",
                "baseline_build_id": str(baseline.id),
                "logical_id": _PREVIEW_TOPIC_LOGICAL_ID,
                "resource_type": _PREVIEW_TOPIC_TYPE,
            }
        },
    )
    return True


# ------------------------------------------------------------------ internal pipeline


async def _run_build(build_id: uuid.UUID) -> None:
    from app.core.db import SessionLocal  # lazy: honors test session patching

    async with SessionLocal() as db:
        build = await db.get(CodegenBuild, build_id)
        if build is None:
            return
        try:
            async with asyncio.timeout(BUILD_TIMEOUT_S):
                await _execute(db, build)
        except TimeoutError:
            await _fail(db, build, "timeout", f"Build exceeded {BUILD_TIMEOUT_S}s budget")
        except (CostCapExceeded, RateLimited) as exc:
            code = "cost_cap" if isinstance(exc, CostCapExceeded) else "rate_limited"
            await _fail(
                db, build, code,
                "Build stopped by platform governance — monthly cap or rate limit reached. "
                "Check My Usage / Admin → Model Controls.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Build %s crashed", build_id)
            await _fail(db, build, "crashed", str(exc))
        finally:
            _cancel_flags.discard(str(build_id))


async def _generate_artifact_set(
    db: AsyncSession, build: CodegenBuild, ctx: BuildCtx, provider
) -> tuple | None:
    """plan → per-file generation → template assembly for the build's CURRENT
    artifact_profile. Returns (plan, generated) or None when cancelled. Called
    once per build normally; twice when size escalation re-plans (13.5AA)."""
    await _set_phase(db, build, "generating", "Planning the artifact set")
    # The resolved profile MUST reach the planner (13.5Z): without it the
    # packaged profile planned with the INLINE prompt (3200-char handlers,
    # stdlib-only) and never force-inserted requirements.txt — the external
    # runner always passed it; this in-process call site did not.
    plan = await provider.plan(ctx, profile=build.artifact_profile)
    total = len(plan.files) + 1  # + template assembly
    await _publish(
        build.id,
        {"type": "plan", "app_name": plan.app_name, "notes": plan.architecture_notes,
         "files": [f.path for f in plan.files] + ["template.json"]},
    )

    # --- per-file generation (R1.2 chunked with budgets)
    generated: dict[str, str] = {}
    for index, planned in enumerate(plan.files, start=1):
        if _cancelled(build.id):
            build.status = "cancelled"
            build.finished_at = _now()
            await db.commit()
            await _publish(build.id, {"type": "done", "status": "cancelled"})
            return None
        await _set_phase(
            db, build, "generating", f"Generating {index}/{total}: {planned.path}"
        )
        content = await provider.generate_file(ctx, plan, planned, generated)
        generated[planned.path] = content
        db.add(
            CodegenArtifact(
                build_id=build.id,
                path=planned.path,
                content=content,
                content_hash=_sha(content),
                size_bytes=len(content.encode()),
                language=planned.language,
            )
        )
        await db.commit()
        await _publish(
            build.id,
            {"type": "file", "path": planned.path, "index": index, "total": total},
        )

    # --- template assembly (the deployable artifact)
    await _set_phase(db, build, "generating", f"Generating {total}/{total}: template.json")
    template_json = await provider.assemble_template(ctx, plan, generated)
    generated["template.json"] = template_json
    db.add(
        CodegenArtifact(
            build_id=build.id,
            path="template.json",
            content=template_json,
            content_hash=_sha(template_json),
            size_bytes=len(template_json.encode()),
            language="json",
        )
    )
    await db.commit()
    await _publish(build.id, {"type": "file", "path": "template.json", "index": total, "total": total})
    return plan, generated


async def _clear_artifacts(db: AsyncSession, build_id: uuid.UUID) -> None:
    """Size escalation re-plans from scratch: the inline attempt's artifact
    rows must not survive beside the packaged set (paths may differ)."""
    from sqlalchemy import delete

    await db.execute(delete(CodegenArtifact).where(CodegenArtifact.build_id == build_id))
    await db.commit()


def _is_size_only_failure(findings: list[Finding]) -> bool:
    """Every finding is an inline-ceiling overflow — nothing else is wrong."""
    return bool(findings) and all(
        f.check == "inline_packaging" and "inline ceiling" in f.message for f in findings
    )


async def _execute(db: AsyncSession, build: CodegenBuild) -> None:
    ctx, forbidden, _slug = await _build_inputs(db, build)
    provider = get_provider(build.provider)
    build.started_at = _now()
    if await _try_preview_overlay(db, build, ctx, forbidden):
        return

    result = await _generate_artifact_set(db, build, ctx, provider)
    if result is None:
        return  # cancelled (terminal state already published)
    plan, generated = result
    template_json = generated["template.json"]

    # Inline-ceiling auto-retry (codegen-quality R2 / FSD B8): when the ONLY
    # thing between this build and ready is generation-length variance, spend
    # one bounded compaction pass before reporting failure. Recorded on the
    # manifest — visible, not hidden (R2.2).
    inline_retry: dict | None = None
    if build.artifact_profile == "inline-cfn":
        pre_findings, _pre = validate_artifacts(
            generated,
            forbidden,
            profile=build.artifact_profile,
            endpoint_auth=ctx.endpoint_auth,
            require_web_console=ctx.require_web_console,
        )
        oversized = [
            f.path for f in plan.files
            if f.path.endswith(".py")
            and len(generated.get(f.path, "")) > MAX_INLINE_CHARS
        ]
        if (
            pre_findings
            and all(f.check == "inline_packaging" for f in pre_findings)
            and oversized
        ):
            inline_retry = {"attempted": True, "files": oversized, "resolved": False}
            previous_sources = {path: generated[path] for path in oversized}
            for index, path in enumerate(oversized, start=1):
                if _cancelled(build.id):
                    build.status = "cancelled"
                    build.finished_at = _now()
                    await db.commit()
                    await _publish(build.id, {"type": "done", "status": "cancelled"})
                    return
                await _set_phase(
                    db, build, "generating",
                    f"Compacting oversized file {index}/{len(oversized)}: {path}",
                )
                planned = next(f for f in plan.files if f.path == path)
                compact = dataclasses.replace(
                    planned,
                    brief=(
                        f"{planned.brief} — COMPACTION RETRY: the previous attempt "
                        f"was {len(generated[path])} chars, over the "
                        f"{MAX_INLINE_CHARS}-char inline ceiling. Regenerate the "
                        "COMPLETE working file under 3400 characters: strip "
                        "comments and docstrings, merge helpers, shorten local "
                        "names — keep exact behavior and the `handler` entry point."
                    ),
                )
                generated[path] = await provider.generate_file(ctx, plan, compact, generated)
                await _replace_artifact(db, build.id, path, generated[path])
                await _publish(
                    build.id,
                    {"type": "file", "path": path, "index": index, "total": len(oversized)},
                )
            await _set_phase(
                db, build, "generating", "Refreshing template.json after compaction"
            )
            template_json = _refresh_inline_sources(
                template_json, previous_sources, generated
            )
            generated["template.json"] = template_json
            await _replace_artifact(db, build.id, "template.json", template_json)
            post_findings, _post = validate_artifacts(
                generated,
                forbidden,
                profile=build.artifact_profile,
                endpoint_auth=ctx.endpoint_auth,
                require_web_console=ctx.require_web_console,
            )
            inline_retry["resolved"] = not post_findings

    manifest_extra: dict = {"inline_retry": inline_retry} if inline_retry else {}

    # Size escalation (13.5AA, product decision 24 Sep 2026 reversing 13.5V's
    # "never chosen silently" for the SIZE case only): when the inline build
    # still fails on NOTHING but the ZipFile ceiling after compaction, re-plan
    # as packaged-cfn with an EMPTY dependency manifest. No third-party code
    # enters — the same generated code ships as an S3 asset instead of inline
    # text — so the supply-chain rationale behind the spec phrase is untouched:
    # the phrase remains the only door to actual dependencies. Recorded on the
    # manifest and in the phase stream; never hidden. In-process builds only.
    if build.artifact_profile == "inline-cfn":
        current_findings, _cur = validate_artifacts(
            generated,
            forbidden,
            profile="inline-cfn",
            endpoint_auth=ctx.endpoint_auth,
            require_web_console=ctx.require_web_console,
        )
        escalatable = (
            _is_size_only_failure(current_findings)
            and bool(get_settings().codegen_workspace_bucket)
        )
        if escalatable:
            oversized_sizes = {
                f.path: len(generated.get(f.path, ""))
                for f in plan.files
                if f.path.endswith(".py") and len(generated.get(f.path, "")) > MAX_INLINE_CHARS
            }
            escalation = {
                "from": "inline-cfn",
                "to": "packaged-cfn",
                "reason": "inline ceiling",
                "oversized": oversized_sizes,
                "resolved": False,
            }
            await _set_phase(
                db, build, "generating",
                "Inline ceiling exceeded — re-planning as a packaged deployment "
                "(code ships as an S3 asset, no inline limit; no dependencies added)",
            )
            await _publish(build.id, {"type": "escalation", **escalation})
            build.artifact_profile = "packaged-cfn"
            await db.commit()
            await _clear_artifacts(db, build.id)
            ctx = dataclasses.replace(ctx, require_packaged=True, size_escalated=True)
            try:
                result = await _generate_artifact_set(db, build, ctx, provider)
            except SizeEscalationDependencyError as exc:
                finding = Finding("packaged_dependencies", "requirements.txt", str(exc))
                build.manifest = {
                    "size_escalation": escalation,
                    "validation": {"findings": [finding.as_dict()]},
                }
                await _fail(
                    db, build, "validation_failed",
                    "1 validation finding(s) — fix the spec and rebuild",
                    findings=[finding.as_dict()],
                )
                return
            if result is None:
                return
            plan, generated = result
            # "resolved" flips to True in _finalize only when the gate passes
            # (live 13.5AA: the first escalated READY build carried False).
            manifest_extra["size_escalation"] = escalation

    if build.artifact_profile == "packaged-cfn":
        # Agent substance R1: validate BEFORE the packaging spend — a broken
        # template must never burn CodeBuild minutes.
        pre_findings, _pre = validate_artifacts(
            generated,
            forbidden,
            profile=build.artifact_profile,
            endpoint_auth=ctx.endpoint_auth,
            require_web_console=ctx.require_web_console,
            declared_connectors=ctx.declared_connectors,
            mcp_tool_connectors=ctx.mcp_tool_connectors,
            agent_dependencies=ctx.agent_dependencies,
            orchestrated=ctx.orchestrated,
            require_memory=ctx.require_memory,
            require_packaged=ctx.require_packaged,
            declared_tools=ctx.declared_tools,
            require_planning=ctx.require_planning,
            loop_iteration_cap=ctx.loop_iteration_cap,
        )
        if pre_findings:
            build.manifest = {
                "validation": {"findings": [f.as_dict() for f in pre_findings]}
            }
            await _fail(
                db, build, "validation_failed",
                f"{len(pre_findings)} validation finding(s) — fix the spec and "
                "rebuild (packaging was not attempted)",
                findings=[f.as_dict() for f in pre_findings],
            )
            return
        await _set_phase(db, build, "generating", "Packaging dependencies on the runner")
        from app.services.codegen import packaging as packaging_svc

        pkg_files = {
            path.rsplit("/", 1)[-1]: content
            for path, content in generated.items()
            if path.startswith("src/") and path.endswith(".py")
        }
        try:
            package_info = await packaging_svc.run_packaging(
                build.id, pkg_files, generated.get("requirements.txt", "")
            )
        except packaging_svc.PackagingFailed as exc:
            await _fail(db, build, "packaging_failed", str(exc))
            return
        # Durable artifacts: the binary zip (S3-backed row) + a browsable
        # SBOM-lite manifest (hashed into the build like any generated file).
        db.add(
            CodegenArtifact(
                build_id=build.id,
                path=contract.PACKAGE_ZIP_KEY,
                content=None,
                s3_key=package_info["artifact_s3_key"],
                content_hash=package_info["zip_sha256"],
                size_bytes=package_info["zip_bytes"],
                language="binary",
            )
        )
        sbom = json.dumps({"packages": package_info["packages"]}, indent=2) + "\n"
        generated[contract.PACKAGE_MANIFEST_KEY] = sbom
        db.add(
            CodegenArtifact(
                build_id=build.id,
                path=contract.PACKAGE_MANIFEST_KEY,
                content=sbom,
                content_hash=_sha(sbom),
                size_bytes=len(sbom.encode()),
                language="json",
            )
        )
        await db.commit()
        await _publish(
            build.id,
            {"type": "file", "path": contract.PACKAGE_ZIP_KEY, "index": None, "total": None},
        )
        manifest_extra["package"] = {
            key: package_info[key]
            for key in ("zip_sha256", "zip_bytes", "packages", "codebuild_id")
        }

    await _finalize(
        db, build,
        generated=generated,
        forbidden=forbidden,
        model_id=ctx.model_id,
        app_name=plan.app_name,
        architecture_notes=plan.architecture_notes,
        endpoint_auth=ctx.endpoint_auth,
        literal_contract=ctx.literal_contract,
        manifest_extra=manifest_extra or None,
    )


def _refresh_inline_sources(
    template_json: str,
    previous_sources: dict[str, str],
    generated: dict[str, str],
) -> str:
    """Replace compacted handler bodies without another model assembly call.

    The first assembled template already passed JSON parsing and contains each
    generated source verbatim. Compaction changes only those exact ZipFile
    values; resource wiring stays byte-for-byte equivalent after re-serialization.
    """
    try:
        document = json.loads(template_json)
    except json.JSONDecodeError as exc:  # defensive: assembly parsed upstream
        raise ValueError(f"Cannot refresh invalid template JSON: {exc}") from exc
    resources = document.get("Resources") if isinstance(document, dict) else None
    if not isinstance(resources, dict):
        raise ValueError("Cannot refresh template without Resources")

    for path, previous in previous_sources.items():
        matches: list[dict] = []
        for resource in resources.values():
            if not isinstance(resource, dict) or resource.get("Type") != "AWS::Lambda::Function":
                continue
            properties = resource.get("Properties")
            code = properties.get("Code") if isinstance(properties, dict) else None
            if isinstance(code, dict) and code.get("ZipFile") == previous:
                matches.append(code)
        if len(matches) != 1:
            raise ValueError(
                f"Compacted source {path} matched {len(matches)} template Lambdas; expected 1"
            )
        matches[0]["ZipFile"] = generated[path]

    return json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"


async def _replace_artifact(
    db: AsyncSession, build_id: uuid.UUID, path: str, content: str
) -> None:
    """Compaction rewrote a file: the browsable artifact row must equal what
    the template embeds (codegen-quality R2.5)."""
    row = (
        await db.execute(
            select(CodegenArtifact).where(
                CodegenArtifact.build_id == build_id, CodegenArtifact.path == path
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.content = content
    row.content_hash = _sha(content)
    row.size_bytes = len(content.encode())
    await db.commit()


# ------------------------------------------------------------------ external pipeline


async def _dispatch_preflight(ctx: BuildCtx) -> None:
    """Dispatch-time governance (external-codegen R6.1): rate bucket + at-cap
    check via the standard seam, then a headroom estimate against the per-build
    token budget (per-call pre-flight is impossible out-of-process)."""
    from app.services.bedrock import InvocationCtx, preflight_model_call
    from app.services.pricing import PRICING_MAP
    from app.services.spend import TRACKER, resolve_caps

    inv_ctx = InvocationCtx(
        purpose="codegen", user_id=ctx.user_id, project_id=ctx.project_id,
        generation_id=ctx.build_id,
    )
    await preflight_model_call(inv_ctx, streaming=False)

    # Headroom estimate: budget priced at the model's output rate (conservative).
    price = PRICING_MAP.get(ctx.model_id)
    if price is None or not TRACKER.hydrated:
        return  # fail-open discipline (S5)
    estimate = price[1] * get_settings().codegen_token_budget / 1000
    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        caps = await resolve_caps(session, ctx.user_id, ctx.project_id)
    if caps.at_cap != "block":
        return
    if ctx.user_id and caps.user_cap:
        mtd = TRACKER.user_mtd.get(ctx.user_id, Decimal("0"))
        if mtd + estimate > caps.user_cap:
            raise CostCapExceeded("user", caps.user_cap, mtd)
    if ctx.project_id and caps.project_cap:
        mtd = TRACKER.project_mtd.get(ctx.project_id, Decimal("0"))
        if mtd + estimate > caps.project_cap:
            raise CostCapExceeded("project", caps.project_cap, mtd)


async def _run_external(build_id: uuid.UUID, *, redispatch: bool = False) -> None:
    """Dispatch (unless re-attaching) then poll to terminal (R4)."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        build = await db.get(CodegenBuild, build_id)
        if build is None:
            return
        try:
            ctx, forbidden, slug = await _build_inputs(db, build)
            provider = get_provider(build.provider)

            if build.external_job_id is None or redispatch:
                await _dispatch_preflight(ctx)
                attempt = 2 if build.retried else 1
                job_id = await provider.dispatch(
                    ctx, slug=slug, artifact_profile=build.artifact_profile, attempt=attempt
                )
                build.external_job_id = job_id
                build.dispatched_at = _now()
                if build.started_at is None:
                    build.started_at = _now()
                await _set_phase(
                    db, build, "dispatched", f"Handed to the {build.provider} runner"
                )

            await _poll_external(db, build, ctx, forbidden)
        except (CostCapExceeded, RateLimited) as exc:
            code = "cost_cap" if isinstance(exc, CostCapExceeded) else "rate_limited"
            await _fail(
                db, build, code,
                "Build stopped by platform governance — monthly cap or rate limit reached. "
                "Check My Usage / Admin → Model Controls.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("External build %s crashed", build_id)
            await _fail(db, build, "crashed", str(exc))


async def _poll_external(
    db: AsyncSession, build: CodegenBuild, ctx: BuildCtx, forbidden: list
) -> None:
    provider = get_provider(build.provider)
    settings = get_settings()
    files_reported: set[str] = set()

    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        await db.refresh(build)
        if build.status in TERMINAL_STATES:  # cancelled via API while we slept
            return

        attempt = 2 if build.retried else 1
        try:
            status = await provider.poll(build.id, attempt)
        except contract.ContractViolation as exc:
            if await _maybe_retry(db, build, ctx, f"contract violation: {exc}"):
                continue
            await _fail(db, build, "contract_violation", str(exc))
            return

        if status.results is None:
            deadline = _aware(build.dispatched_at)
            timed_out = (
                deadline is not None
                and (_now() - deadline).total_seconds() > settings.codegen_external_timeout_s
            )
            if timed_out:
                if await _maybe_retry(db, build, ctx, "engine timed out"):
                    continue
                await _fail(
                    db, build, "engine_timeout",
                    f"No results within {settings.codegen_external_timeout_s}s",
                )
                return
            if status.started and build.status == "dispatched":
                await _set_phase(db, build, "generating", "Engine generating")
            for path in status.files_seen:
                if path not in files_reported:
                    files_reported.add(path)
                    await _publish(
                        build.id,
                        {"type": "file", "path": path, "index": len(files_reported),
                         "total": None},
                    )
            continue

        # --- results present → ingest
        if status.results.status == "failed":
            error = status.results.error or {}
            await _fail(
                db, build,
                str(error.get("code", "engine_failure"))[:40],
                str(error.get("message", "Engine reported failure"))[:1000],
            )
            return
        try:
            await _ingest(db, build, ctx, forbidden, status.results)
        except contract.ContractViolation as exc:
            if await _maybe_retry(db, build, ctx, f"contract violation: {exc}"):
                continue
            await _fail(db, build, "contract_violation", str(exc))
        return


async def _maybe_retry(
    db: AsyncSession, build: CodegenBuild, ctx: BuildCtx, reason: str
) -> bool:
    """One automatic re-dispatch on engine_timeout/contract_violation (R4.4)."""
    if build.retried:
        return False
    logger.warning("build %s re-dispatching after: %s", build.id, reason)
    build.retried = True
    provider = get_provider(build.provider)
    _ctx, _forbidden, slug = await _build_inputs(db, build)
    job_id = await provider.dispatch(ctx, slug=slug, artifact_profile=build.artifact_profile, attempt=2)
    build.external_job_id = job_id
    build.dispatched_at = _now()
    await _set_phase(db, build, "dispatched", "Re-dispatched to the runner (retry 1/1)")
    return True


async def _ingest(
    db: AsyncSession,
    build: CodegenBuild,
    ctx: BuildCtx,
    forbidden: list,
    results: contract.Results,
) -> None:
    """Hash-verified download → artifact rows → priced usage → the S8 gate (R5/R6.2)."""
    provider = get_provider(build.provider)
    attempt = 2 if build.retried else 1
    generated = await provider.fetch_artifacts(build.id, attempt, results)

    await db.refresh(build)
    if build.status in TERMINAL_STATES:  # cancelled while downloading
        return

    def _language(path: str) -> str:
        for suffix, lang in ((".json", "json"), (".py", "python"), (".ts", "typescript"),
                             (".zip", "binary")):
            if path.endswith(suffix):
                return lang
        return "markdown"

    if build.artifact_profile == "cdk-app":
        # S3 is the single source of truth for cdk-app content (S10 R3):
        # server-side copy from the expiring workspace to the artifacts prefix.
        from app.services.codegen import storage

        keys = await storage.copy_response_to_artifacts(
            build_id=build.id,
            attempt_root=provider.prefix_root(build.id, attempt),
            paths=[entry.path for entry in results.files],
        )
        for entry in results.files:
            db.add(
                CodegenArtifact(
                    build_id=build.id, path=entry.path, content=None,
                    s3_key=keys[entry.path], content_hash=entry.sha256,
                    size_bytes=entry.bytes, language=_language(entry.path),
                )
            )
    else:
        for entry in results.files:
            db.add(
                CodegenArtifact(
                    build_id=build.id,
                    path=entry.path,
                    content=generated[entry.path],
                    content_hash=entry.sha256,
                    size_bytes=entry.bytes,
                    language=_language(entry.path),
                )
            )
    await db.commit()

    priced_usd = await _reconcile_usage(db, build, ctx, results)

    manifest_extra = {
        "engine": results.engine,
        "external_job_id": build.external_job_id,
        "retried": build.retried,
        "artifact_profile": build.artifact_profile,
        "reconciliation": {
            "token_budget": get_settings().codegen_token_budget,
            "reported_input_tokens": sum(u.input_tokens for u in results.usage),
            "reported_output_tokens": sum(u.output_tokens for u in results.usage),
            "priced_usd": float(priced_usd),
        },
    }
    if results.synth_assets:
        manifest_extra["synth"] = {
            "assets": [
                {
                    "id": a.id, "path": a.path, "source_hash": a.source_hash,
                    "bucket_parameter": a.bucket_parameter,
                    "key_parameter": a.key_parameter,
                    "hash_parameter": a.hash_parameter,
                }
                for a in results.synth_assets
            ]
        }

    await _finalize(
        db, build,
        generated=generated,
        forbidden=forbidden,
        model_id=ctx.model_id,
        app_name=results.app_name or ctx.project_name,
        architecture_notes=results.architecture_notes or "",
        endpoint_auth=ctx.endpoint_auth,
        literal_contract=ctx.literal_contract,
        manifest_extra=manifest_extra,
    )


async def _reconcile_usage(
    db: AsyncSession, build: CodegenBuild, ctx: BuildCtx, results: contract.Results
) -> Decimal:
    """Runner-reported usage → priced model_invocations rows (source='runner')
    + cap accounting (external-codegen R6.2). Never raises."""
    from app.models import ModelInvocation
    from app.services import spend as spend_svc
    from app.services.pricing import compute_cost_usd

    total = Decimal("0")
    try:
        marker = f"[external runner {results.engine.get('name', 'unknown')}]"
        marker_sha = _sha(marker)
        for entry in results.usage:
            cost = compute_cost_usd(entry.model_id, entry.input_tokens, entry.output_tokens)
            db.add(
                ModelInvocation(
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    generation_id=build.id,
                    purpose="codegen",
                    source="runner",
                    model_id=entry.model_id,
                    prompt_text=marker,
                    prompt_sha256=marker_sha,
                    response_text="",
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    stop_reason=None,
                    latency_ms=0,
                    cost_usd=cost,
                    success=True,
                )
            )
            if cost:
                total += cost
        await db.commit()
        if total > 0:
            caps = await spend_svc.resolve_caps(db, ctx.user_id, ctx.project_id)
            crossings = await spend_svc.TRACKER.add(ctx.user_id, ctx.project_id, total, caps)
            if crossings:
                await spend_svc.record_crossings(db, crossings)
    except Exception:  # noqa: BLE001 — reconciliation must not void a good build
        logger.exception("usage reconciliation failed for build %s", build.id)
    return total


# ------------------------------------------------------------------ queries


async def latest_ready_build(db: AsyncSession, project_id: uuid.UUID) -> CodegenBuild | None:
    return (
        await db.execute(
            select(CodegenBuild)
            .where(CodegenBuild.project_id == project_id, CodegenBuild.status == "ready")
            .order_by(CodegenBuild.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def rehydrate_inflight_builds() -> None:
    """Restart recovery: in-process builds fail loudly (the work died with the
    process, S2 pattern); external builds RE-ATTACH — the job continues in
    CodeBuild, we just resume polling (external-codegen R4.3)."""
    from app.core.db import SessionLocal  # lazy: honors test session patching

    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(CodegenBuild).where(CodegenBuild.status.in_(ACTIVE_STATES))
            )
            reattach: list[uuid.UUID] = []
            for build in result.scalars():
                if build.external_job_id is not None:
                    reattach.append(build.id)
                    continue
                build.status = "failed"
                build.error = {
                    "code": "restarted",
                    "message": "Interrupted by a platform restart — start a new build",
                    "findings": [],
                }
                build.finished_at = _now()
            await db.commit()
        for build_id in reattach:
            logger.info("re-attaching to external build %s", build_id)
            _spawn(build_id, _run_external(build_id))
    except Exception as exc:  # pragma: no cover — first boot, tables may not exist
        logger.warning("Codegen rehydration skipped: %s", exc)
