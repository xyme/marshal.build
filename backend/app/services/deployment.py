"""Deployment orchestrator (S1-08 deploy, S1-09 status/logs, S1-10 teardown).

State machine (FSD §4.5.2 subset):
    pending → pre_flight → deploying → active → tearing_down → torn_down
    failed reachable from pre_flight / deploying / tearing_down
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.sandbox as sandbox_service
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import CodegenBuild, Deployment, Lease, Project, User

logger = logging.getLogger("marshal.deploy")

TERMINAL_STATES = {"active", "failed", "torn_down", "superseded"}
IN_FLIGHT_STATES = {"pending", "pre_flight", "leasing", "deploying", "updating", "tearing_down"}
POLL_INTERVAL_S = 4
CFN_FAILURE_STATUSES = {
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "ROLLBACK_IN_PROGRESS",
    "DELETE_FAILED",
}
UPDATE_SUCCESS = "UPDATE_COMPLETE"
UPDATE_ROLLED_BACK = "UPDATE_ROLLBACK_COMPLETE"
UPDATE_STUCK = "UPDATE_ROLLBACK_FAILED"
# S11 R5 defaults — platform_settings.deployment_policies overrides when set (S12)
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 168
LEASE_CLAMP_HOURS = 6  # platform teardown always precedes ISB reaping (R6.3)


def get_sandbox_provider():
    """Resolve the configured provider late so runtime/test overrides are honored."""
    return sandbox_service.get_sandbox_provider()


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def provider_for_lease(lease: Lease | None):
    """B20 R0.3: an EXISTING lease is operated by the provider that created it
    (its recorded `provider` name), never the boot-time global — switching
    SANDBOX_PROVIDER between deploy and teardown must not strand old leases.

    Falls back to the current provider when the lease carries no name or the
    current provider is nameless (test fakes)."""
    provider = get_sandbox_provider()
    name = getattr(lease, "provider", None)
    if not name or getattr(provider, "name", name) == name:
        return provider
    from app.services.sandbox import provider_by_name

    return provider_by_name(name)


# ------------------------------------------------------------------ event bus


class DeploymentEventBus:
    """In-memory pub/sub fanning deployment events to SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, deployment_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subscribers.setdefault(deployment_id, []).append(queue)
        return queue

    async def unsubscribe(self, deployment_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(deployment_id, [])
            if queue in subs:
                subs.remove(queue)
            if not subs:
                self._subscribers.pop(deployment_id, None)

    async def publish(self, deployment_id: str, event: dict) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(deployment_id, []))
        for queue in subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # slow consumer; drop oldest
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass


event_bus = DeploymentEventBus()
_tasks: dict[str, asyncio.Task] = {}


def _spawn(deployment_id: str, coro) -> None:
    task = asyncio.create_task(coro)
    _tasks[deployment_id] = task
    task.add_done_callback(lambda t: _tasks.pop(deployment_id, None))


# ------------------------------------------------------------------ helpers


def load_sample_template() -> str:
    settings = get_settings()
    path = Path(settings.sample_app_template_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path.read_text()


async def _record(
    db: AsyncSession,
    deployment: Deployment,
    *,
    status: str | None = None,
    phase_detail: str | None = None,
    **fields: Any,
) -> None:
    """Persist a state/timeline change and publish it to subscribers."""
    if status:
        deployment.status = status
        deployment.timeline = [
            *(deployment.timeline or []),
            {"phase": status, "at": _now_iso(), "detail": phase_detail},
        ]
    for key, value in fields.items():
        setattr(deployment, key, value)
    await db.commit()
    if status:
        await event_bus.publish(
            str(deployment.id),
            {"type": "phase", "phase": status, "detail": phase_detail, "at": _now_iso()},
        )
    if status in ("active", "failed"):
        # Owner notification on terminal outcomes (notifications spec, §4.4.6 default: both)
        from app.services import notifications as notif

        succeeded = status == "active"
        notif.emit(
            notif.emit_for_user(
                deployment.user_id,
                type="deploy_succeeded" if succeeded else "deploy_failed",
                title="Deployment live" if succeeded else "Deployment failed",
                body=(
                    f"Your app is live in its Enclave: {deployment.app_url}"
                    if succeeded and deployment.app_url
                    else (phase_detail or deployment.error or "See the deployment console for details.")
                ),
                link=f"/projects/{deployment.project_id}",
                dedupe_key=f"deploy:{deployment.id}:{status}",
            )
        )


def _stack_name(project_id: uuid.UUID) -> str:
    return f"{get_settings().deployment_stack_prefix}-{project_id.hex[:12]}"


# ------------------------------------------------------------------ deploy flow


async def resolve_ttl_hours(db: AsyncSession) -> tuple[int, int]:
    """(default_ttl, max_ttl) — S12 policy values when present, S11 defaults else."""
    from app.models import PlatformSettings

    try:
        row = await db.get(PlatformSettings, 1)
        policies = (getattr(row, "deployment_policies", None) or {}) if row else {}
        default_ttl = int(policies.get("default_ttl_hours", DEFAULT_TTL_HOURS))
        max_ttl = int(policies.get("max_ttl_hours", MAX_TTL_HOURS))
        return default_ttl, max_ttl
    except Exception:  # noqa: BLE001 — policy trouble never blocks deploys
        return DEFAULT_TTL_HOURS, MAX_TTL_HOURS


# One stable platform-wide key serializes all admissions. The two-int form
# gives this subsystem a readable namespace and avoids Python's randomized hash.
DEPLOYMENT_ADMISSION_LOCK_NAMESPACE = 0x4D415253  # "MARS"
DEPLOYMENT_ADMISSION_LOCK_KEY = 0x4445504C  # "DEPL"


async def _acquire_deployment_admission_lock(db: AsyncSession) -> None:
    """Acquire the PostgreSQL transaction lock; local/test dialects are no-op.

    PostgreSQL releases pg_advisory_xact_lock automatically on commit or
    rollback, including pooled-connection reuse. SQLite is single-process test
    infrastructure here; correctness in the multi-task cloud path is the lock.
    """
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {
            "namespace": DEPLOYMENT_ADMISSION_LOCK_NAMESPACE,
            "key": DEPLOYMENT_ADMISSION_LOCK_KEY,
        },
    )


async def _enforce_provider_capacity(*, policies, is_update: bool) -> None:
    """Fresh provider-capacity check while the admission lock is held.

    Provider-aware (install drill G14). `direct` has no external pool: its
    capacity IS the policy concurrency limits (max_concurrent_platform /
    per-user) that enforce_deploy_capacity already applied in this same
    transaction under this same lock, so it admits with no further check and
    there is no provider uncertainty to fail closed on. `isb` keeps the
    fail-closed /accounts snapshot and provider_capacity_buffer check. Any
    other provider name still fails closed. The complete-policy-snapshot
    requirement (admissions_paused) applies to both and is enforced upstream.
    """
    if is_update:
        return
    from app.services.policies import PolicyViolation

    provider = get_sandbox_provider()
    provider_name = getattr(provider, "name", "unknown")
    cloud = get_settings().environment.strip().lower() == "cloud"
    if not cloud:
        return
    if provider_name == "direct":
        return
    if provider_name != "isb":
        raise PolicyViolation(
            "provider_capacity_unavailable",
            f"Cloud deployment admission has no capacity rule for sandbox provider "
            f"'{provider_name}'; only 'direct' and 'isb' are supported.",
        )
    snapshot_fn = getattr(provider, "capacity_snapshot", None)
    if snapshot_fn is None:
        raise PolicyViolation(
            "provider_capacity_unavailable",
            "The sandbox provider cannot prove available account capacity.",
        )
    try:
        snapshot = await snapshot_fn()
    except Exception as exc:
        logger.exception("sandbox provider capacity snapshot failed")
        raise PolicyViolation(
            "provider_capacity_unavailable",
            "Sandbox account capacity is temporarily unverifiable; admission is "
            "blocked rather than risking pool exhaustion.",
        ) from exc
    if snapshot is None:
        raise PolicyViolation(
            "provider_capacity_unavailable",
            "The cloud sandbox provider returned no trusted capacity snapshot.",
        )
    if snapshot.available_accounts <= policies.provider_capacity_buffer:
        raise PolicyViolation(
            "provider_capacity_buffer",
            f"The sandbox pool has {snapshot.available_accounts} available account(s); "
            f"at least {policies.provider_capacity_buffer} must remain available after "
            "admission. Try again after an account finishes cleanup.",
        )


async def start_deployment(
    db: AsyncSession, user: User, project: Project, build=None,
    mode: str = "full_governance",
) -> Deployment:
    """Atomically admit a create or update-in-place deployment attempt.

    The API retains its cheap capacity preflight before risk scoring. This
    service takes the cross-task transaction lock, rechecks project state,
    global/per-user concurrency and active quota, then commits the pending row
    before the lock is released. Updates inherit their active stack and remain
    exempt from capacity quotas.
    """
    active = None
    try:
        await _acquire_deployment_admission_lock(db)

        in_flight = await db.execute(
            select(Deployment).where(
                Deployment.project_id == project.id,
                Deployment.status.in_(IN_FLIGHT_STATES),
            )
        )
        if in_flight.scalar_one_or_none() is not None:
            raise ValueError("A deployment is already in progress for this project")
        active = (
            await db.execute(
                select(Deployment).where(
                    Deployment.project_id == project.id, Deployment.status == "active"
                )
            )
        ).scalar_one_or_none()

        from app.services.policies import (
            count_provider_reservations,
            enforce_deploy_capacity,
            resolve_deployment_policies,
        )

        policies = await resolve_deployment_policies(db)
        if active is None:
            # Standing blast-radius quota uses the same physical reservation
            # definition as platform/user concurrency. Check it first to
            # preserve the established per-user refusal when both bounds are
            # reached; update-in-place remains exempt.
            quota = policies.max_active_deployments_per_user
            if quota > 0:
                held = await count_provider_reservations(db, user.id)
                if held >= quota:
                    raise ValueError(
                        f"You already hold {held} live deployment(s) — the per-user "
                        f"limit is {quota}. Tear down one you no longer need, or ask "
                        "an admin to raise the limit."
                    )

        await enforce_deploy_capacity(
            db, user.id, is_update=active is not None, policies=policies
        )
        await _enforce_provider_capacity(
            policies=policies, is_update=active is not None
        )

        deployment = Deployment(
            project_id=project.id,
            user_id=user.id,
            build_id=build.id if build is not None else None,
            status="pending",
            timeline=[],
            resources=[],
            mode=mode,
        )
        if active is not None:
            # In-place update: inherit the running stack + lease and immutable
            # custody mode; the prior row is superseded only on update success.
            deployment.stack_name = active.stack_name
            deployment.stack_id = active.stack_id
            deployment.lease_id = active.lease_id
            deployment.mode = active.mode
        db.add(deployment)
        # The pending reservation and all authoritative reads share this
        # transaction. Commit atomically publishes it and releases the lock.
        await db.commit()
    except Exception:
        # Release a transaction-scoped lock immediately on every refusal/error;
        # session teardown remains a backstop for connection/cancellation paths.
        await db.rollback()
        raise

    await db.refresh(deployment)
    if active is not None:
        _spawn(str(deployment.id), _run_update(deployment.id, active.id))
    else:
        _spawn(str(deployment.id), _run_deploy(deployment.id))
    return deployment


class DeployPayload:
    """Resolved deployment payload (S8 R5.1 seam, S10 asset-aware)."""

    def __init__(self, template: str, assets: list[dict] | None = None):
        self.template = template
        self.assets = assets or []  # [{path, s3_key, staged_key, parameters{...}, source_hash}]


async def _resolve_payload(db: AsyncSession, deployment: Deployment) -> DeployPayload:
    """Payload seam: sample app | inline-cfn build | cdk-app build (staged assets)."""
    return await _payload_for_build(db, deployment.build_id)


async def _payload_for_build(db: AsyncSession, build_id: uuid.UUID | None) -> "DeployPayload":
    """Template + asset bindings for a build id (shared by deploy and the
    S18+ changeset preview — one resolution, no drift)."""
    if not build_id:
        return DeployPayload(load_sample_template())
    from app.models import CodegenArtifact, CodegenBuild
    from app.services.codegen import storage

    build = await db.get(CodegenBuild, build_id)
    template_path = (
        "synth/template.json" if build and build.artifact_profile == "cdk-app"
        else "template.json"
    )
    artifact = (
        await db.execute(
            select(CodegenArtifact).where(
                CodegenArtifact.build_id == build_id,
                CodegenArtifact.path == template_path,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise ValueError(f"Build artifact {template_path} is missing")
    template = await storage.artifact_text(artifact)
    if build and build.artifact_profile == "packaged-cfn":
        # Agent substance R1: ONE package zip, bound to the two fixed
        # parameters with a PLAIN key (no CDK '||' packaging convention,
        # no hash parameter — the hash lives in the manifest + artifact row).
        row = (
            await db.execute(
                select(CodegenArtifact).where(
                    CodegenArtifact.build_id == build.id,
                    CodegenArtifact.path == "package.zip",
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.s3_key:
            raise ValueError("Build artifact package.zip is missing from storage")
        return DeployPayload(
            template,
            [
                {
                    "s3_key": row.s3_key,
                    "staged_key": f"marshal-assets/{row.content_hash}.zip",
                    "source_hash": row.content_hash,
                    "bucket_parameter": "PackageBucket",
                    "key_parameter": "PackageKey",
                    "hash_parameter": None,
                    "plain_key": True,
                }
            ],
        )
    if not build or build.artifact_profile != "cdk-app":
        return DeployPayload(template)

    assets: list[dict] = []
    for entry in ((build.manifest or {}).get("synth") or {}).get("assets", []):
        row = (
            await db.execute(
                select(CodegenArtifact).where(
                    CodegenArtifact.build_id == build.id,
                    CodegenArtifact.path == entry["path"],
                )
            )
        ).scalar_one_or_none()
        if row is None or not row.s3_key:
            raise ValueError(f"Asset artifact {entry['path']} is missing from storage")
        assets.append(
            {
                "s3_key": row.s3_key,
                "staged_key": f"marshal-assets/{entry['source_hash']}.zip",
                "source_hash": entry["source_hash"],
                "bucket_parameter": entry["bucket_parameter"],
                "key_parameter": entry["key_parameter"],
                "hash_parameter": entry["hash_parameter"],
            }
        )
    return DeployPayload(template, assets)


async def _stage_assets(session, account_id: str, payload: DeployPayload) -> list[dict]:
    """Upload asset zips into an Enclave-local staging bucket via the assumed
    role; return the CFN Parameters binding them (cdk-artifacts R4.1).

    Bucket ensure + upload happen BEFORE create_stack — a blueprint missing S3
    rights fails here with a named error, never mid-stack (R4.5).
    """
    from app.services.codegen import storage as platform_storage

    bucket = f"marshal-assets-{account_id}"
    s3 = session.client("s3")

    def ensure_bucket() -> None:
        try:
            s3.create_bucket(Bucket=bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise ValueError(
                    f"blueprint_outdated: cannot create staging bucket {bucket} "
                    f"({code}) — the Enclave blueprint may predate S10 "
                    "(docs/innovation-sandbox-setup.md)"
                ) from exc

    await asyncio.to_thread(ensure_bucket)
    parameters: list[dict] = []
    for asset in payload.assets:
        body = await platform_storage.artifact_bytes_by_key(asset["s3_key"])
        await asyncio.to_thread(
            lambda a=asset, b=body: s3.put_object(Bucket=bucket, Key=a["staged_key"], Body=b)
        )
        parameters += _asset_parameters(asset, bucket)
    return parameters


def _asset_parameters(asset: dict, bucket: str) -> list[dict]:
    """CFN parameter bindings for one staged asset. CDK synth assets carry a
    hash parameter and the 'key||' packaging convention; packaged-cfn assets
    (agent-substance R1) bind a plain key and no hash parameter."""
    key_value = (
        asset["staged_key"] if asset.get("plain_key") else f"{asset['staged_key']}||"
    )
    parameters = [
        {"ParameterKey": asset["bucket_parameter"], "ParameterValue": bucket},
        {"ParameterKey": asset["key_parameter"], "ParameterValue": key_value},
    ]
    if asset.get("hash_parameter"):
        parameters.append(
            {
                "ParameterKey": asset["hash_parameter"],
                "ParameterValue": asset["source_hash"],
            }
        )
    return parameters


async def _run_deploy(deployment_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        deployment = await db.get(Deployment, deployment_id)
        if deployment is None:
            return
        provider = get_sandbox_provider()
        lease: Lease | None = None
        try:
            await _record(
                db, deployment, status="pre_flight",
                phase_detail=(
                    "Validating project (deploying generated build)"
                    if deployment.build_id else "Validating project"
                ),
            )
            payload = await _resolve_payload(db, deployment)  # fails fast if missing
            template = payload.template

            # --- Lease (S1-07). With the ISB provider this blocks until the pool
            # account is leased + blueprint-provisioned (can take minutes).
            account_noun = "Testbed" if deployment.mode == "testbed" else "Enclave"
            await _record(
                db,
                deployment,
                status="leasing",
                phase_detail=f"Requesting {account_noun} account via provider '{provider.name}'",
            )
            # S12 R1.4: record the policy budget on the lease row. ISB lease
            # templates carry a fixed budget — per-lease overrides are an ISB
            # API gap tracked in ADR 001 (OQ-7); the platform row is the truth
            # the costs dashboard and future reaper read.
            # B20 R0.2: the row is committed BEFORE the provider's blocking
            # activation, and the external id lands via on_created the moment
            # ISB returns it — process death cannot orphan a provider lease
            # with no platform record (rehydration reaps by external id).
            from app.services.policies import resolve_deployment_policies

            deploy_policies = await resolve_deployment_policies(db)
            lease = Lease(
                provider=provider.name,
                status="requested",
                project_id=deployment.project_id,
                user_id=deployment.user_id,
                budget_usd=deploy_policies.per_deployment_budget_usd,
            )
            db.add(lease)
            await db.flush()
            deployment.lease_id = lease.id
            await db.commit()

            async def _persist_external_id(info) -> None:
                lease.external_lease_id = info.external_lease_id
                lease.aws_account_id = info.aws_account_id
                await db.commit()

            lease_info = await provider.request_lease(
                project_id=str(deployment.project_id),
                user_id=str(deployment.user_id),
                on_created=_persist_external_id,
            )
            lease.external_lease_id = lease_info.external_lease_id
            lease.aws_account_id = lease_info.aws_account_id
            lease.status = lease_info.status
            lease.activated_at = _now() if lease_info.status == "active" else None
            await _record(
                db,
                deployment,
                status="deploying",
                phase_detail=f"Lease {lease_info.external_lease_id} on account {lease_info.aws_account_id}",
            )

            # --- CloudFormation create (S1-08); S10: stage CDK assets first
            lease_info.deployment_id = str(deployment.id)  # STS session attribution
            session = await asyncio.to_thread(provider.deployment_session, lease_info)
            cfn = session.client("cloudformation")
            # C1: connector copies land BEFORE stack create — generated code
            # reads them at cold start (fail-closed on custody trouble).
            copied = await _ensure_connector_secrets(db, deployment, session)
            if copied:
                await _record(
                    db, deployment, status="deploying",
                    phase_detail=f"Provisioned {copied} connector secret(s) into the account",
                )
            # Composable agents R2.2: dependency endpoints + keys ride the
            # same custody pattern (cross-lease read, copy into this account).
            wired = await _ensure_dependency_secrets(db, deployment, session)
            if wired:
                await _record(
                    db, deployment, status="deploying",
                    phase_detail=f"Wired {wired} agent dependenc{'y' if wired == 1 else 'ies'}",
                )
            parameters: list[dict] = []
            if payload.assets:
                await _record(
                    db, deployment, status="deploying",
                    phase_detail=f"Staging {len(payload.assets)} asset(s) into the Enclave",
                )
                parameters = await _stage_assets(
                    session, lease_info.aws_account_id, payload
                )
            stack_name = _stack_name(deployment.project_id)
            tags = [
                {"Key": "marshal-ai:project-id", "Value": str(deployment.project_id)},
                {"Key": "marshal-ai:user-id", "Value": str(deployment.user_id)},
                {"Key": "marshal-ai:deployment-id", "Value": str(deployment.id)},
                {"Key": "marshal-ai:managed", "Value": "true"},
            ]

            def create() -> str:
                resp = cfn.create_stack(
                    StackName=stack_name,
                    TemplateBody=template,
                    Parameters=parameters,
                    Capabilities=["CAPABILITY_IAM"],
                    Tags=tags,
                    OnFailure="DO_NOTHING",  # we capture reason, then delete ourselves
                )
                return resp["StackId"]

            stack_id = await asyncio.to_thread(create)
            deployment.stack_name = stack_name
            deployment.stack_id = stack_id
            await db.commit()

            # --- Poll to completion (S1-09)
            final_status = await _poll_stack(db, deployment, cfn, until_deleted=False)
            if final_status == "CREATE_COMPLETE":
                outputs = await _stack_outputs(cfn, stack_name)
                app_url = outputs.get("ApiUrl")
                healthy = await _health_check(app_url) if app_url else False
                default_ttl, _ = await resolve_ttl_hours(db)
                await _record(
                    db,
                    deployment,
                    status="active",
                    phase_detail="Healthy" if healthy else "Deployed (health check degraded)",
                    app_url=app_url,
                    deployed_at=_now(),
                    # S11 R5: expires_at enforced by the sweeper; lease clamp
                    # applies on read/extend (ISB lease TTL is 30d ≫ policy TTLs)
                    expires_at=_now() + timedelta(hours=default_ttl),
                    health="healthy" if healthy else "unknown",
                    last_health_at=_now(),
                )
                # S16-02: the health probe proves "/" answers; the smoke pass
                # proves the ROUTES the template declares answer too. B13: a
                # keyed API is smoked WITH its key — 403s must not pass vacuously.
                api_key_expected = bool(outputs.get("ApiKeyId"))
                api_key = await _fetch_api_key(session, outputs)
                if api_key:
                    await _record(
                        db, deployment, status="active",
                        phase_detail="API key active — endpoints require x-api-key",
                    )
                elif api_key_expected:
                    await _record(
                        db,
                        deployment,
                        status="active",
                        phase_detail=(
                            "API key expected but its value is not readable yet — "
                            "smoke will report inconclusive, never run keyless"
                        ),
                    )
                await _stage_web_console(db, deployment, session, outputs)  # B19
                await _post_deploy_smoke(
                    db,
                    deployment,
                    template,
                    api_key=api_key,
                    api_key_expected=api_key_expected,
                )
            else:
                reason = await _first_failure_reason(cfn, stack_name)
                await asyncio.to_thread(cfn.delete_stack, StackName=stack_name)  # rollback
                await _record(
                    db,
                    deployment,
                    status="failed",
                    phase_detail=f"Stack {final_status}",
                    error=reason or f"CloudFormation status {final_status}",
                )
        except Exception as exc:
            logger.exception("Deployment %s failed", deployment_id)
            # B20 R0.2 / G26: a lease this attempt holds is released whenever
            # the failure landed BEFORE a stack existed — nothing in the
            # account is left for teardown to reclaim, and an unterminated row
            # counts against the user's and the platform's capacity forever
            # (count_provider_reservations). `requested` (never activated) and
            # `active` (direct activates immediately; ISB once provisioned) are
            # released alike, through the same provider seam teardown uses:
            # ISB returns the pooled account, direct is bookkeeping only.
            # Once create_stack returned a StackId the lease stays — the stack
            # exists and its teardown remains the user's call.
            if (
                lease is not None
                and not deployment.stack_id
                and not deployment.stack_name
                and lease.status != "terminated"
            ):
                try:
                    if lease.external_lease_id:
                        await provider.terminate_lease(lease.external_lease_id)
                    lease.status = "terminated"
                    lease.terminated_at = _now()
                    deployment.timeline = [
                        *(deployment.timeline or []),
                        {
                            "phase": "lease_released",
                            "at": _now_iso(),
                            "detail": "lease released — deployment failed before stack creation",
                        },
                    ]
                    await db.commit()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "lease %s cleanup failed (provider reaping backstops)", lease.id
                    )
            await _record(db, deployment, status="failed", error=str(exc), phase_detail=str(exc))
        finally:
            await event_bus.publish(str(deployment_id), {"type": "done", "at": _now_iso()})


async def preview_update(db: AsyncSession, project, build) -> dict:
    """Changeset preview for an in-place redeploy (S11 backlog item).

    Creates a CloudFormation change set against the ACTIVE stack with the
    candidate payload, reads the planned changes, deletes the change set —
    the stack itself is never modified. Asset parameters are synthesized from
    their deterministic staged keys WITHOUT uploading (preview must not write
    into the Enclave); create_change_set does not dereference S3 values.
    """
    active = (
        await db.execute(
            select(Deployment).where(
                Deployment.project_id == project.id, Deployment.status == "active"
            )
        )
    ).scalar_one_or_none()
    if active is None:
        raise ValueError("No active deployment — preview applies to in-place updates only")
    lease = await db.get(Lease, active.lease_id) if active.lease_id else None
    if lease is None:
        raise ValueError("Active deployment has no lease")

    payload = await _payload_for_build(db, build.id if build is not None else None)
    from app.services.sandbox.base import LeaseInfo

    provider = provider_for_lease(lease)
    session = await asyncio.to_thread(
        provider.deployment_session,
        LeaseInfo(
            external_lease_id=lease.external_lease_id,
            aws_account_id=lease.aws_account_id,
            status="active",
            deployment_id=str(active.id),
        ),
    )
    cfn = session.client("cloudformation")
    bucket = f"marshal-assets-{lease.aws_account_id}"
    parameters = []
    for asset in payload.assets:
        parameters += _asset_parameters(asset, bucket)

    change_set_name = f"marshal-preview-{uuid.uuid4().hex[:8]}"

    def create() -> None:
        cfn.create_change_set(
            StackName=active.stack_name,
            ChangeSetName=change_set_name,
            ChangeSetType="UPDATE",
            TemplateBody=payload.template,
            Parameters=parameters,
            Capabilities=["CAPABILITY_IAM"],
        )

    def describe() -> dict:
        return cfn.describe_change_set(
            StackName=active.stack_name, ChangeSetName=change_set_name
        )

    def cleanup() -> None:
        try:
            cfn.delete_change_set(
                StackName=active.stack_name, ChangeSetName=change_set_name
            )
        except ClientError:  # already gone — preview cleanup is best-effort
            pass

    await asyncio.to_thread(create)
    try:
        detail: dict = {}
        for _ in range(30):  # ≤30s — change set evaluation is fast for our stack sizes
            detail = await asyncio.to_thread(describe)
            if detail.get("Status") in ("CREATE_COMPLETE", "FAILED"):
                break
            await asyncio.sleep(1)
        if detail.get("Status") == "FAILED":
            reason = detail.get("StatusReason", "")
            if "didn't contain changes" in reason or "No updates" in reason:
                return {"no_changes": True, "changes": [], "stack_name": active.stack_name}
            raise ValueError(f"Change set failed: {reason[:300]}")
        if detail.get("Status") != "CREATE_COMPLETE":
            raise ValueError("Change set evaluation timed out — try again")

        changes = []
        pages = detail
        while True:
            for change in pages.get("Changes", []):
                rc = change.get("ResourceChange", {})
                changes.append(
                    {
                        "action": rc.get("Action"),
                        "logical_id": rc.get("LogicalResourceId"),
                        "resource_type": rc.get("ResourceType"),
                        "replacement": rc.get("Replacement"),
                        "scope": rc.get("Scope", []),
                    }
                )
            token = pages.get("NextToken")
            if not token:
                break
            pages = await asyncio.to_thread(
                lambda t=token: cfn.describe_change_set(
                    StackName=active.stack_name, ChangeSetName=change_set_name, NextToken=t
                )
            )
        return {"no_changes": False, "changes": changes, "stack_name": active.stack_name}
    finally:
        await asyncio.to_thread(cleanup)


async def _run_update(deployment_id: uuid.UUID, prior_id: uuid.UUID) -> None:
    """In-place redeploy (S11 R2/R3): update_stack on the running stack.

    Outcomes mirror what CloudFormation actually runs afterwards:
      UPDATE_COMPLETE          → new row active, prior superseded
      UPDATE_ROLLBACK_COMPLETE → new row failed, prior STAYS active (rollback)
      UPDATE_ROLLBACK_FAILED   → new row failed, prior degraded + admin alert
    """
    async with SessionLocal() as db:
        deployment = await db.get(Deployment, deployment_id)
        prior = await db.get(Deployment, prior_id)
        if deployment is None or prior is None:
            return
        try:
            await _record(
                db, deployment, status="pre_flight",
                phase_detail="Preparing in-place update of the running stack",
            )
            payload = await _resolve_payload(db, deployment)
            lease = await db.get(Lease, deployment.lease_id) if deployment.lease_id else None
            if lease is None:
                raise ValueError("Active deployment has no lease to update within")
            provider = provider_for_lease(lease)
            from app.services.sandbox.base import LeaseInfo

            lease_info = LeaseInfo(
                external_lease_id=lease.external_lease_id,
                aws_account_id=lease.aws_account_id,
                status="active",
                deployment_id=str(deployment.id),
            )
            session = await asyncio.to_thread(provider.deployment_session, lease_info)
            cfn = session.client("cloudformation")
            # C1: re-put connector copies on update — registry values may have
            # rotated since the prior deploy (create-or-put is idempotent).
            copied = await _ensure_connector_secrets(db, deployment, session)
            if copied:
                await _record(
                    db, deployment, status="updating",
                    phase_detail=f"Refreshed {copied} connector secret(s) in the account",
                )
            # R2.2: dependency edges refresh too (endpoints/keys may have moved)
            wired = await _ensure_dependency_secrets(db, deployment, session)
            if wired:
                await _record(
                    db, deployment, status="updating",
                    phase_detail=f"Refreshed {wired} agent dependency edge(s)",
                )
            parameters: list[dict] = []
            if payload.assets:
                await _record(
                    db, deployment, status="updating",
                    phase_detail=f"Staging {len(payload.assets)} asset(s) into the Enclave",
                )
                parameters = await _stage_assets(session, lease_info.aws_account_id, payload)

            def update() -> None:
                cfn.update_stack(
                    StackName=deployment.stack_name,
                    TemplateBody=payload.template,
                    Parameters=parameters,
                    Capabilities=["CAPABILITY_IAM"],
                )

            try:
                await asyncio.to_thread(update)
            except ClientError as exc:
                if "No updates are to be performed" in str(exc):
                    await _record(
                        db, deployment, status="failed",
                        phase_detail="Nothing changed",
                        error="No changes to deploy — the running stack already matches this payload",
                    )
                    return
                raise
            if deployment.status != "updating":
                await _record(db, deployment, status="updating", phase_detail="Applying stack update")

            final = await _poll_stack(db, deployment, cfn, until_deleted=False, updating=True)
            if final == UPDATE_SUCCESS:
                outputs = await _stack_outputs(cfn, deployment.stack_name)
                default_ttl, _ = await resolve_ttl_hours(db)
                prior.status = "superseded"
                prior.superseded_at = _now()
                prior.timeline = [
                    *(prior.timeline or []),
                    {"phase": "superseded", "at": _now_iso(),
                     "detail": f"Replaced in place by attempt {deployment.id}"},
                ]
                await _record(
                    db, deployment, status="active",
                    phase_detail="Update complete",
                    app_url=outputs.get("ApiUrl") or prior.app_url,
                    deployed_at=_now(),
                    expires_at=_now() + timedelta(hours=default_ttl),
                    health="unknown",
                )
                api_key_expected = bool(outputs.get("ApiKeyId"))
                api_key = await _fetch_api_key(session, outputs)  # B13
                await _stage_web_console(db, deployment, session, outputs)  # B19
                await _post_deploy_smoke(
                    db,
                    deployment,
                    payload.template,
                    api_key=api_key,
                    api_key_expected=api_key_expected,
                )  # S16-02
            elif final == UPDATE_STUCK:
                prior.health = "degraded"
                await db.commit()
                from app.models import Alert

                db.add(Alert(
                    kind="sandbox", severity="critical",
                    scope_project_id=deployment.project_id,
                    message=(
                        f"Stack {deployment.stack_name} is UPDATE_ROLLBACK_FAILED — "
                        "manual teardown required (S11 R2.4)"
                    ),
                    dedupe_key=f"stuck:{deployment.id}",
                ))
                await db.commit()
                await _record(
                    db, deployment, status="failed",
                    phase_detail="Update rollback FAILED — stack needs teardown",
                    error="CloudFormation could not roll back; tear down and redeploy",
                )
            else:  # rolled back — the previous build still serves (R2.1)
                reason = await _first_failure_reason(cfn, deployment.stack_name)
                await _record(
                    db, deployment, status="failed",
                    phase_detail="Update rolled back — previous build still serving",
                    error=reason or f"CloudFormation status {final}",
                )
        except Exception as exc:
            logger.exception("Update %s failed", deployment_id)
            await _record(db, deployment, status="failed", error=str(exc), phase_detail=str(exc))
        finally:
            await event_bus.publish(str(deployment_id), {"type": "done", "at": _now_iso()})


class DependentsBlockTeardown(Exception):
    """Composable agents R4.3: live callers depend on this agent."""

    def __init__(self, dependents: list[str]):
        self.dependents = dependents
        super().__init__(
            "Live agents depend on this deployment: "
            + ", ".join(dependents)
            + " — tear those down first, or they will be calling a dead endpoint"
        )


async def start_teardown(
    db: AsyncSession, deployment: Deployment, *, force: bool = False
) -> None:
    if deployment.status not in {"active", "failed"}:
        raise ValueError(f"Cannot tear down a deployment in state '{deployment.status}'")
    if not force:
        # R4.3 default: refuse and NAME the dependents. The TTL sweeper passes
        # force=True — expiry is a policy contract that outranks composition,
        # and the dependents' own health probes surface the dead edge.
        from app.services.composition import dependents_with_active_deployments

        dependents = await dependents_with_active_deployments(
            db, deployment.project_id
        )
        if dependents:
            raise DependentsBlockTeardown(dependents)
    await _record(db, deployment, status="tearing_down", phase_detail="Deleting stack")
    _spawn(str(deployment.id), _run_teardown(deployment.id))


async def _run_teardown(deployment_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        deployment = await db.get(Deployment, deployment_id)
        if deployment is None:
            return
        try:
            lease = await db.get(Lease, deployment.lease_id) if deployment.lease_id else None
            provider = provider_for_lease(lease)
            lease_info = None
            if lease:
                from app.services.sandbox.base import LeaseInfo

                lease_info = LeaseInfo(
                    external_lease_id=lease.external_lease_id,
                    aws_account_id=lease.aws_account_id,
                    status=lease.status,
                    deployment_id=str(deployment.id),
                )
            session = (
                await asyncio.to_thread(provider.deployment_session, lease_info)
                if lease_info
                else None
            )
            if deployment.stack_name and session:
                cfn = session.client("cloudformation")
                # B19 R4.2: a non-empty console bucket blocks stack deletion —
                # empty it first (best-effort; account recycling backstops).
                await _empty_web_bucket(session, cfn, deployment.stack_name)
                # C1: connector copies die with the deployment (ISB only —
                # direct mode shares the account with other deployments).
                await _delete_connector_secrets(db, deployment, session)
                await _delete_dependency_secrets(db, deployment, session)
                await asyncio.to_thread(cfn.delete_stack, StackName=deployment.stack_name)
                final = await _poll_stack(db, deployment, cfn, until_deleted=True)
                if final not in {"DELETE_COMPLETE", "STACK_GONE"}:
                    await _record(
                        db,
                        deployment,
                        status="failed",
                        error=f"Teardown failed: {final}",
                        phase_detail=f"Teardown failed: {final}",
                    )
                    return
            # S10: best-effort staged-asset cleanup (account recycling backstops)
            if deployment.build_id and session:
                try:
                    payload = await _resolve_payload(db, deployment)
                    if payload.assets and lease_info and lease_info.aws_account_id:
                        s3 = session.client("s3")
                        bucket = f"marshal-assets-{lease_info.aws_account_id}"
                        for asset in payload.assets:
                            await asyncio.to_thread(
                                lambda a=asset: s3.delete_object(
                                    Bucket=bucket, Key=a["staged_key"]
                                )
                            )
                except Exception:  # noqa: BLE001
                    logger.info("staged-asset cleanup skipped (recycling backstops)")
            if lease:
                if lease.external_lease_id:
                    await provider.terminate_lease(lease.external_lease_id)
                lease.status = "terminated"
                lease.terminated_at = _now()
            await _record(
                db,
                deployment,
                status="torn_down",
                phase_detail="All resources deleted",
                torn_down_at=_now(),
            )
            # The stack and lease are GONE, so no sibling row that shared them
            # can still be live. Failed in-place updates leave the prior row
            # active (S11 rollback) and attempts share stack_name + lease_id —
            # without this, tearing down one row left the other claiming
            # "active" over deleted infrastructure, and the next deploy took
            # the update path into an account the platform no longer holds
            # (observed live 27 Aug, demo reseed).
            siblings = (
                await db.execute(
                    select(Deployment).where(
                        Deployment.project_id == deployment.project_id,
                        Deployment.id != deployment.id,
                        Deployment.status.in_(("active", "failed", "updating")),
                    )
                )
            ).scalars().all()
            for sibling in siblings:
                shares_stack = (
                    deployment.stack_name and sibling.stack_name == deployment.stack_name
                )
                shares_lease = (
                    deployment.lease_id and sibling.lease_id == deployment.lease_id
                )
                if shares_stack or shares_lease:
                    await _record(
                        db,
                        sibling,
                        status="torn_down",
                        phase_detail=(
                            "Torn down with the shared stack "
                            f"(teardown of attempt {str(deployment.id)[:8]})"
                        ),
                        torn_down_at=_now(),
                    )
        except Exception as exc:
            logger.exception("Teardown %s failed", deployment_id)
            await _record(db, deployment, status="failed", error=str(exc), phase_detail=str(exc))
        finally:
            await event_bus.publish(str(deployment_id), {"type": "done", "at": _now_iso()})


# ------------------------------------------------------------------ CFN polling


async def _poll_stack(
    db: AsyncSession, deployment: Deployment, cfn, *, until_deleted: bool,
    updating: bool = False,
) -> str:
    """Poll stack status + stream new events until terminal. Returns final status."""
    seen_events: set[str] = set()
    stack_ref = deployment.stack_id or deployment.stack_name
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        try:
            stacks = await asyncio.to_thread(
                lambda: cfn.describe_stacks(StackName=stack_ref)["Stacks"]
            )
            status = stacks[0]["StackStatus"]
        except ClientError as exc:
            if "does not exist" in str(exc):
                return "STACK_GONE" if until_deleted else "CREATE_FAILED"
            raise

        await _stream_new_events(db, deployment, cfn, stack_ref, seen_events)

        if until_deleted:
            if status == "DELETE_COMPLETE":
                return status
            if status in {"DELETE_FAILED"}:
                return status
        elif updating:  # S11 R3: in-place update terminal states
            if status in {UPDATE_SUCCESS, UPDATE_ROLLED_BACK, UPDATE_STUCK, "UPDATE_FAILED"}:
                return status
        else:
            if status in {"CREATE_COMPLETE"}:
                return status
            if status in CFN_FAILURE_STATUSES:
                return status


async def _stream_new_events(
    db: AsyncSession, deployment: Deployment, cfn, stack_ref: str, seen: set[str]
) -> None:
    try:
        events = await asyncio.to_thread(
            lambda: cfn.describe_stack_events(StackName=stack_ref)["StackEvents"]
        )
    except ClientError:
        return
    resources: dict[str, dict] = {r["logical_id"]: r for r in (deployment.resources or [])}
    fresh = [e for e in reversed(events) if e["EventId"] not in seen]
    for event in fresh:
        seen.add(event["EventId"])
        payload = {
            "type": "cfn",
            "at": event["Timestamp"].isoformat(),
            "logical_id": event.get("LogicalResourceId"),
            "resource_type": event.get("ResourceType"),
            "status": event.get("ResourceStatus"),
            "reason": event.get("ResourceStatusReason"),
        }
        await event_bus.publish(str(deployment.id), payload)
        if event.get("ResourceType") != "AWS::CloudFormation::Stack":
            resources[event["LogicalResourceId"]] = {
                "logical_id": event["LogicalResourceId"],
                "type": event["ResourceType"],
                "status": event["ResourceStatus"],
            }
    if fresh:
        deployment.resources = list(resources.values())
        await db.commit()


async def _stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    stacks = await asyncio.to_thread(lambda: cfn.describe_stacks(StackName=stack_name)["Stacks"])
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


async def fetch_live_api_key(db: AsyncSession, project) -> dict:
    """B13 owner reveal (integration-wave R1.4): fetch the key value LIVE from
    the Enclave — stack outputs → ApiKeyId → value — through the same assumed
    role that deployed. Nothing is stored platform-side; the caller audits.

    Raises ValueError (no active deployment / no lease → 409) or LookupError
    (deployment carries no API key → 404)."""
    active = (
        await db.execute(
            select(Deployment).where(
                Deployment.project_id == project.id, Deployment.status == "active"
            )
        )
    ).scalar_one_or_none()
    if active is None or not active.stack_name:
        raise ValueError("No active deployment to read a key from")
    lease = await db.get(Lease, active.lease_id) if active.lease_id else None
    if lease is None:
        raise ValueError("Active deployment has no lease")
    from app.services.sandbox.base import LeaseInfo

    provider = provider_for_lease(lease)
    session = await asyncio.to_thread(
        provider.deployment_session,
        LeaseInfo(
            external_lease_id=lease.external_lease_id,
            aws_account_id=lease.aws_account_id,
            status="active",
            deployment_id=str(active.id),
        ),
    )
    cfn = session.client("cloudformation")
    outputs = await _stack_outputs(cfn, active.stack_name)
    if not outputs.get("ApiKeyId"):
        raise LookupError(
            "This deployment has no API key — it may be an explicit-public or legacy build"
        )
    value = await _fetch_api_key(session, outputs)
    if value is None:
        raise ValueError("Key fetch failed — the Enclave role may lack apigateway:GET")
    return {
        "deployment_id": str(active.id),
        "api_key_id": outputs["ApiKeyId"],
        "value": value,
    }


AGENT_CONNECTOR_SECRET_PREFIX = "marshal/agent-connectors/"


async def _declared_connectors_for(db: AsyncSession, deployment: Deployment) -> list[str]:
    """C1: the build manifest is deploy truth (frozen at gate time). Old
    builds predate the key ⇒ no connectors, no re-parse."""
    if not deployment.build_id:
        return []
    from app.models import CodegenBuild

    build = await db.get(CodegenBuild, deployment.build_id)
    manifest = (build.manifest or {}) if build else {}
    return [str(s) for s in (manifest.get("declared_connectors") or [])]


async def _ensure_connector_secrets(
    db: AsyncSession, deployment: Deployment, session
) -> int:
    """C1 COPY custody (owner decision, 7 Sep 2026): provision the composite
    connector payload {"base_url","header","value"} into the TARGET account's
    Secrets Manager at marshal/agent-connectors/<slug> BEFORE stack create —
    generated code reads it at cold start. Runs on deploy AND update (registry
    values may have rotated); changeset preview never calls this. Failure is
    FAIL-CLOSED: an agent generated to call connectors must not deploy
    without them. The distinct prefix keeps direct-mode copies from
    overwriting the registry's own marshal/connectors/ custody."""
    declared = await _declared_connectors_for(db, deployment)
    if not declared:
        return 0
    from app.models import PlatformSettings
    from app.services import connectors as connectors_svc

    row = await db.get(PlatformSettings, 1)
    entries = {e.get("slug"): e for e in ((row.connectors if row else None) or [])}
    sm = session.client("secretsmanager")
    for slug in declared:
        entry = entries.get(slug)
        if entry is None or not entry.get("active", True):
            raise ValueError(
                f"Declared connector '{slug}' is not registered and active — "
                "register/reactivate it in Admin → Integrations, or rebuild "
                "without the declaration"
            )
        credential = await connectors_svc.credential_for(slug)
        payload = json.dumps(
            {
                "base_url": entry.get("base_url"),
                "header": entry.get("auth_header") or "Authorization",
                "value": credential,  # null when no credential is registered
            }
        )
        name = f"{AGENT_CONNECTOR_SECRET_PREFIX}{slug}"

        def _put(n=name, p=payload) -> None:
            try:
                sm.create_secret(Name=n, SecretString=p)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                    raise
                sm.put_secret_value(SecretId=n, SecretString=p)

        try:
            await asyncio.to_thread(_put)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "AccessDenied",
                "AccessDeniedException",
            ):
                # The _stage_assets blueprint_outdated precedent: name the
                # real cause instead of a bare AccessDenied.
                raise ValueError(
                    "Connector custody was denied in the target account — the "
                    "Enclave blueprint needs the marshal/agent-connectors "
                    "Secrets Manager grant (blueprint_outdated). Roll out the "
                    "updated blueprint StackSet, then retry."
                ) from exc
            raise
    return len(declared)


AGENT_DEPENDENCY_SECRET_PREFIX = "marshal/agent-dependencies/"


async def _ensure_dependency_secrets(
    db: AsyncSession, deployment: Deployment, session
) -> int:
    """Composable agents R2.2 (COPY custody, the C1 pattern): for each
    declared dependency, read its LIVE endpoint + API key through ITS OWN
    lease session (cross-lease read — the fetch_live_api_key mechanics), and
    write the composite {"base_url","header","value"} into the CALLER's
    account at marshal/agent-dependencies/<slug>. Fail-closed: an agent
    generated to call other agents must not deploy without its edges.
    Explicit-public dependencies get value=null (keyless call).
    Runs on deploy AND update; preview never calls this."""
    project = await db.get(Project, deployment.project_id)
    dependencies = (
        ((project.composition if project else None) or {}).get("dependencies") or []
    )
    if not dependencies:
        return 0
    from app.services.sandbox.base import LeaseInfo

    sm = session.client("secretsmanager")
    for dep in dependencies:
        slug = str(dep.get("slug"))
        dep_project_id = uuid.UUID(str(dep.get("project_id")))
        active = (
            await db.execute(
                select(Deployment).where(
                    Deployment.project_id == dep_project_id,
                    Deployment.status == "active",
                )
            )
        ).scalar_one_or_none()
        if active is None or not active.app_url:
            raise ValueError(
                f"Declared agent dependency '{slug}' has no active deployment — "
                "deploy it first (the preflight should have refused; the "
                "dependency may have been torn down mid-deploy)"
            )
        # Cross-lease key read: the DEPENDENCY's lease session, not ours.
        value = None
        dep_lease = await db.get(Lease, active.lease_id) if active.lease_id else None
        if dep_lease is not None and active.stack_name:
            dep_provider = provider_for_lease(dep_lease)
            dep_session = await asyncio.to_thread(
                dep_provider.deployment_session,
                LeaseInfo(
                    external_lease_id=dep_lease.external_lease_id,
                    aws_account_id=dep_lease.aws_account_id,
                    status="active",
                    deployment_id=str(active.id),
                ),
            )
            dep_cfn = dep_session.client("cloudformation")
            dep_outputs = await _stack_outputs(dep_cfn, active.stack_name)
            if dep_outputs.get("ApiKeyId"):
                value = await _fetch_api_key(dep_session, dep_outputs)
                if value is None:
                    raise ValueError(
                        f"Dependency '{slug}' is keyed but its key could not be "
                        "read — retry, or check the Enclave role's apigateway:GET"
                    )
        payload = json.dumps(
            {"base_url": active.app_url, "header": "x-api-key", "value": value}
        )
        name = f"{AGENT_DEPENDENCY_SECRET_PREFIX}{slug}"

        def _put(n=name, p=payload) -> None:
            try:
                sm.create_secret(Name=n, SecretString=p)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                    raise
                sm.put_secret_value(SecretId=n, SecretString=p)

        try:
            await asyncio.to_thread(_put)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "AccessDenied",
                "AccessDeniedException",
            ):
                raise ValueError(
                    "Dependency custody was denied in the target account — the "
                    "Enclave blueprint needs the marshal/agent-dependencies "
                    "Secrets Manager grant (blueprint_outdated). Roll out the "
                    "updated blueprint StackSet, then retry."
                ) from exc
            raise
    return len(dependencies)


async def _delete_dependency_secrets(db: AsyncSession, deployment: Deployment, session) -> None:
    """Teardown twin — ISB only, best-effort (the connector-twin semantics)."""
    try:
        lease = await db.get(Lease, deployment.lease_id) if deployment.lease_id else None
        if lease is None or lease.provider != "isb":
            return
        project = await db.get(Project, deployment.project_id)
        dependencies = (
            ((project.composition if project else None) or {}).get("dependencies") or []
        )
        if not dependencies:
            return
        sm = session.client("secretsmanager")
        for dep in dependencies:
            await asyncio.to_thread(
                lambda s=str(dep.get("slug")): sm.delete_secret(
                    SecretId=f"{AGENT_DEPENDENCY_SECRET_PREFIX}{s}",
                    ForceDeleteWithoutRecovery=True,
                )
            )
    except Exception:  # noqa: BLE001 — recycling backstops
        logger.info("dependency-secret cleanup skipped (recycling backstops)")


async def _delete_connector_secrets(db: AsyncSession, deployment: Deployment, session) -> None:
    """Teardown twin (best-effort): ISB accounts get recycled anyway; DIRECT
    mode skips deletion because the account is shared — another active
    deployment may declare the same slug, and the control-plane account is
    already trusted custody."""
    try:
        lease = await db.get(Lease, deployment.lease_id) if deployment.lease_id else None
        if lease is None or lease.provider != "isb":
            return
        declared = await _declared_connectors_for(db, deployment)
        if not declared:
            return
        sm = session.client("secretsmanager")
        for slug in declared:
            await asyncio.to_thread(
                lambda s=slug: sm.delete_secret(
                    SecretId=f"{AGENT_CONNECTOR_SECRET_PREFIX}{s}",
                    ForceDeleteWithoutRecovery=True,
                )
            )
    except Exception:  # noqa: BLE001 — recycling backstops
        logger.info("connector-secret cleanup skipped (recycling backstops)")


async def _stage_web_console(
    db: AsyncSession, deployment: Deployment, session, outputs: dict[str, str]
) -> None:
    """B19 R4.1: put the build's web/* artifacts into the console bucket
    through the assumed role — BEFORE smoke, so the /app probe exercises the
    real page. Failure never fails the deployment (smoke shows /app instead)."""
    bucket = (outputs or {}).get("WebBucketName")
    if not bucket or not deployment.build_id:
        return
    try:
        from app.models import CodegenArtifact

        rows = (
            await db.execute(
                select(CodegenArtifact).where(
                    CodegenArtifact.build_id == deployment.build_id,
                    CodegenArtifact.path.like("web/%"),
                )
            )
        ).scalars().all()
        if not rows:
            return
        s3 = session.client("s3")
        for row in rows:
            key = row.path[len("web/"):]
            content_type = "text/html" if key.endswith(".html") else "text/plain"
            await asyncio.to_thread(
                lambda r=row, k=key, ct=content_type: s3.put_object(
                    Bucket=bucket, Key=k, Body=(r.content or "").encode(),
                    ContentType=ct,
                )
            )
        deployment.timeline = [
            *(deployment.timeline or []),
            {
                "phase": "smoke", "at": _now_iso(),
                "detail": f"Test console staged ({len(rows)} file(s)) — /app",
            },
        ]
        await db.commit()
    except Exception:  # noqa: BLE001 — staging trouble surfaces via the /app smoke probe
        logger.warning("web console staging failed for %s", deployment.id, exc_info=True)


async def _empty_web_bucket(session, cfn, stack_name: str) -> None:
    """B19 R4.2: best-effort console-bucket emptying pre-delete (a non-empty
    bucket wedges delete_stack; account recycling backstops any miss)."""
    try:
        outputs = await _stack_outputs(cfn, stack_name)
        bucket = outputs.get("WebBucketName")
        if not bucket:
            return
        s3 = session.client("s3")

        def _empty() -> None:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})

        await asyncio.to_thread(_empty)
    except Exception:  # noqa: BLE001
        logger.info("web-bucket emptying skipped (recycling backstops)")


async def _fetch_api_key(session, outputs: dict[str, str]) -> str | None:
    """B13 (integration-wave R1.3): the key VALUE, fetched through the same
    assumed-role session that deployed the stack, held in memory only — the
    platform stores nothing (per-deployment custody: the key lives and dies
    with the stack). Returns None when the stack has no ApiKeyId output or
    the fetch fails (smoke then runs keyless, exactly as before B13)."""
    key_id = (outputs or {}).get("ApiKeyId")
    if not key_id:
        return None
    try:
        apigw = session.client("apigateway")
        resp = await asyncio.to_thread(
            lambda: apigw.get_api_key(apiKey=key_id, includeValue=True)
        )
        return resp.get("value")
    except Exception:  # noqa: BLE001 — key fetch must not fail the deployment
        logger.warning("API key fetch failed for key id %s", key_id, exc_info=True)
        return None


async def _first_failure_reason(cfn, stack_name: str) -> str | None:
    try:
        events = await asyncio.to_thread(
            lambda: cfn.describe_stack_events(StackName=stack_name)["StackEvents"]
        )
    except ClientError:
        return None
    for event in reversed(events):
        if "FAILED" in str(event.get("ResourceStatus", "")):
            return f"{event.get('LogicalResourceId')}: {event.get('ResourceStatusReason')}"
    return None


async def _health_check(url: str, attempts: int = 5) -> bool:
    """Initial liveness after CREATE/UPDATE_COMPLETE.

    Alive means "something answers below 500" — the SAME rule the 15-minute
    lifecycle prober applies (deployment_lifecycle._probe). These disagreed
    until 5 Aug 2026: this check demanded 200 on "/", so a REST API with no
    root method deployed as "health check degraded" and then flipped healthy
    at the next tick (live showcase finding — the false alarm class, not a
    real outage). Auth refusals and 403 root responses are an ALIVE gateway.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt in range(attempts):
            try:
                response = await client.get(url)
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(min(2**attempt, 8))
    return False


# ------------------------------------------------------------ S16-02 smoke

SMOKE_MAX_ROUTES = 8


def derive_smoke_routes(template: str) -> tuple[list[str], list[str]]:
    """(probe_paths, skipped_routes) from the CFN template the stack actually
    ran — the S12 drill finding: the static gate proves the template is
    allowed, not that the app behind it answers.

    Handles the shapes the generator emits (validate.py allowlist):
    ApiGatewayV2 RouteKeys ("GET /x", "ANY /x", "$default") are probed for
    GET-able keys; non-GET keys are recorded as skipped, not silently dropped.
    """
    probe: list[str] = ["/"]
    skipped: list[str] = []
    try:
        resources = (json.loads(template) or {}).get("Resources", {})
    except (TypeError, ValueError):
        return probe, skipped

    # --- HTTP API shape (ApiGatewayV2 RouteKeys)
    for resource in resources.values():
        if resource.get("Type") != "AWS::ApiGatewayV2::Route":
            continue
        key = str((resource.get("Properties") or {}).get("RouteKey", ""))
        if key == "$default":
            continue  # catch-all — the root probe already exercises it
        parts = key.split(" ", 1)
        if len(parts) != 2 or not parts[1].startswith("/"):
            continue
        method, path = parts[0].upper(), parts[1]
        if "{" in path:  # parameterized — no value to substitute honestly
            skipped.append(key)
            continue
        if method in ("GET", "ANY"):
            if path not in probe:
                probe.append(path)
        else:
            skipped.append(key)

    # --- REST API shape (ApiGateway Resource tree + Methods) — what the
    # inline-cfn generator actually emits. Missing until 5 Aug 2026: smoke
    # silently probed only "/" on REST templates and "passed (1 route(s))"
    # while every declared route went unexercised (live showcase finding).
    rest_paths: dict[str, str | None] = {}  # logical id -> resolved path

    def _resolve_rest_path(logical_id: str, depth: int = 0) -> str | None:
        if depth > 12 or logical_id not in resources:
            return None
        if logical_id in rest_paths:
            return rest_paths[logical_id]
        props = resources[logical_id].get("Properties") or {}
        part = str(props.get("PathPart", ""))
        parent = props.get("ParentId")
        parent_path = ""
        if isinstance(parent, dict) and "Ref" in parent:
            resolved = _resolve_rest_path(str(parent["Ref"]), depth + 1)
            if resolved is None:
                rest_paths[logical_id] = None
                return None
            parent_path = "" if resolved == "/" else resolved
        # non-Ref ParentId (GetAtt RootResourceId) → parent is the API root
        path = f"{parent_path}/{part}" if part else "/"
        rest_paths[logical_id] = path
        return path

    for resource in resources.values():
        if resource.get("Type") != "AWS::ApiGateway::Method":
            continue
        props = resource.get("Properties") or {}
        method = str(props.get("HttpMethod", "")).upper()
        resource_ref = props.get("ResourceId")
        if not (isinstance(resource_ref, dict) and "Ref" in resource_ref):
            continue  # method on the API root — the "/" probe covers it
        path = _resolve_rest_path(str(resource_ref["Ref"]))
        if path is None:
            continue
        key = f"{method} {path}"
        if "{" in path:
            skipped.append(key)
            continue
        if method in ("GET", "ANY"):
            if path not in probe:
                probe.append(path)
        else:
            skipped.append(key)
    return probe[:SMOKE_MAX_ROUTES], skipped


async def _post_deploy_smoke(
    db: AsyncSession,
    deployment: Deployment,
    template: str,
    *,
    api_key: str | None = None,
    api_key_expected: bool = False,
) -> None:
    """Probe manifest-derived routes after CREATE/UPDATE_COMPLETE.

    Keyed builds are never probed keyless: if ApiKeyId exists but value fetch
    fails, record an explicit inconclusive report and let owner reveal retry.
    """
    if not deployment.app_url:
        return
    if api_key_expected and not api_key:
        detail = (
            "Smoke inconclusive — stack requires an API key but the value "
            "could not be fetched yet; re-check with Reveal key"
        )
        report = {
            "at": _now_iso(),
            "deployment_id": str(deployment.id),
            "probed": [],
            "skipped_routes": [],
            "passed": None,
            "auth_pending": 1,
            "reason": "api_key_value_unavailable",
        }
        deployment.timeline = [
            *(deployment.timeline or []),
            {"phase": "smoke", "at": _now_iso(), "detail": detail},
        ]
        await db.commit()
        await event_bus.publish(
            str(deployment.id),
            {"type": "phase", "phase": "smoke", "detail": detail, "at": _now_iso()},
        )
        if deployment.build_id:
            build = await db.get(CodegenBuild, deployment.build_id)
            if build is not None:
                build.manifest = {**(build.manifest or {}), "smoke": report}
                await db.commit()
        return
    paths, skipped = derive_smoke_routes(template)
    base = deployment.app_url.rstrip("/")
    results: list[dict] = []
    failures = 0
    propagation_deadline: float | None = None  # shared across routes (B13)
    headers = {"x-api-key": api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        for path in paths:
            entry: dict = {"path": path}
            try:
                response = await client.get(f"{base}{path}")
                entry["status"] = response.status_code
                declared = path != "/"
                entry["ok"] = response.status_code < 500 and not (
                    declared and response.status_code == 404
                )
                # B13: WE hold the key, so a keyed refusal is either a broken
                # usage-plan binding or API GW's key-association propagation.
                # The STRUCTURAL class is caught statically by the auth_contract
                # gate (ApiStages required), and live runs measured propagation
                # from ~5 to >10 minutes — so smoke retries within one shared
                # budget and, if still refused, reports INCONCLUSIVE (timeline
                # warning, no degraded flag): a verdict that cries wolf on
                # every keyed deploy would train operators to ignore it.
                # Keyless 403 stays a pass (auth working as intended).
                if api_key and declared and response.status_code in (401, 403):
                    deadline = propagation_deadline or (time.monotonic() + 600)
                    propagation_deadline = deadline
                    while time.monotonic() < deadline:
                        await asyncio.sleep(30)
                        retry = await client.get(f"{base}{path}")
                        entry["status"] = retry.status_code
                        if retry.status_code not in (401, 403):
                            entry["ok"] = retry.status_code < 500 and not (
                                declared and retry.status_code == 404
                            )
                            break
                    else:
                        entry["ok"] = False
                        entry["auth_pending"] = True
                        entry["error"] = (
                            "still refusing the api key — likely API GW key "
                            "propagation; verify with the revealed key "
                            "(persistent 403s = broken usage-plan binding)"
                        )
            except httpx.HTTPError as exc:
                entry["status"] = None
                entry["ok"] = False
                entry["error"] = str(exc)[:120]
            if not entry["ok"]:
                failures += 1
            results.append(entry)

    # B13: routes still refusing the key at budget exhaustion are
    # INCONCLUSIVE (propagation), not failures — the timeline says so and
    # health stays untouched; genuinely broken bindings are the gate's job.
    auth_pending = sum(1 for r in results if r.get("auth_pending"))
    failures -= auth_pending

    report = {
        "at": _now_iso(),
        "deployment_id": str(deployment.id),
        "probed": results,
        "skipped_routes": skipped,
        "passed": failures == 0,
        "auth_pending": auth_pending,
    }
    detail = (
        f"Smoke passed ({len(results)} route(s))"
        if failures == 0 and not auth_pending
        else (
            f"Smoke inconclusive on {auth_pending}/{len(results)} route(s) — "
            "API key propagation; re-check with the revealed key"
            if failures == 0
            else f"Smoke FAILED on {failures}/{len(results)} route(s)"
        )
    )
    deployment.timeline = [
        *(deployment.timeline or []),
        {"phase": "smoke", "at": _now_iso(), "detail": detail},
    ]
    if failures:
        deployment.health = "degraded"
        deployment.last_health_at = _now()
    await db.commit()
    await event_bus.publish(
        str(deployment.id), {"type": "phase", "phase": "smoke", "detail": detail, "at": _now_iso()}
    )

    # Surface the finding on the build card (build.manifest.smoke)
    if deployment.build_id:
        build = await db.get(CodegenBuild, deployment.build_id)
        if build is not None:
            build.manifest = {**(build.manifest or {}), "smoke": report}
            await db.commit()
    if failures:
        from app.services import notifications as notif

        notif.emit(
            notif.emit_for_user(
                deployment.user_id,
                type="deployment_degraded",
                title="Deployed, but smoke probes failed",
                body=f"{detail} — the app is up yet some routes do not answer. "
                "See the build card for the per-route report.",
                link=f"/projects/{deployment.project_id}",
                dedupe_key=f"smoke:{deployment.id}",
            )
        )


# ------------------------------------------------------------------ rehydration


async def rehydrate_inflight_deployments() -> None:
    """Resume polling for deployments left non-terminal by a previous process (R3.5)."""
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(Deployment).where(Deployment.status.notin_(TERMINAL_STATES))
            )
            inflight = list(result.scalars())
    except Exception as exc:  # DB may not exist yet on first boot
        logger.warning("Rehydration skipped: %s", exc)
        return
    for deployment in inflight:
        logger.info("Rehydrating deployment %s (status=%s)", deployment.id, deployment.status)
        if deployment.status == "tearing_down":
            _spawn(str(deployment.id), _run_teardown(deployment.id))
        elif deployment.status in {"pending", "pre_flight", "leasing"}:
            async with SessionLocal() as db:
                dep = await db.get(Deployment, deployment.id)
                # B20 R0.2: the lease row now exists BEFORE activation — one
                # carrying an external id but never activated may be live on
                # the provider side. Reap best-effort (provider TTL backstops).
                lease = await db.get(Lease, dep.lease_id) if dep.lease_id else None
                if lease and lease.external_lease_id and lease.status == "requested":
                    try:
                        await provider_for_lease(lease).terminate_lease(
                            lease.external_lease_id
                        )
                        lease.status = "terminated"
                        lease.terminated_at = _now()
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "orphaned lease %s termination failed "
                            "(provider reaping backstops)",
                            lease.id,
                        )
                await _record(
                    db, dep, status="failed", error="Interrupted before stack creation",
                    phase_detail="Interrupted by restart"
                )
        else:  # deploying
            _spawn(str(deployment.id), _resume_deploy_poll(deployment.id))


async def _resume_deploy_poll(deployment_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        deployment = await db.get(Deployment, deployment_id)
        if deployment is None or not deployment.stack_name:
            return
        try:
            lease = await db.get(Lease, deployment.lease_id) if deployment.lease_id else None
            provider = provider_for_lease(lease)
            from app.services.sandbox.base import LeaseInfo

            lease_info = LeaseInfo(
                external_lease_id=lease.external_lease_id if lease else None,
                aws_account_id=lease.aws_account_id if lease else None,
                status="active",
                deployment_id=str(deployment.id),
            )
            session = await asyncio.to_thread(provider.deployment_session, lease_info)
            cfn = session.client("cloudformation")
            final_status = await _poll_stack(db, deployment, cfn, until_deleted=False)
            if final_status == "CREATE_COMPLETE":
                outputs = await _stack_outputs(cfn, deployment.stack_name)
                app_url = outputs.get("ApiUrl")
                await _record(
                    db, deployment, status="active", app_url=app_url, deployed_at=_now(),
                    phase_detail="Resumed and completed",
                )
            else:
                await _record(
                    db, deployment, status="failed",
                    error=f"CloudFormation status {final_status}",
                    phase_detail=f"Stack {final_status}",
                )
        except Exception as exc:
            await _record(db, deployment, status="failed", error=str(exc), phase_detail=str(exc))
        finally:
            await event_bus.publish(str(deployment_id), {"type": "done", "at": _now_iso()})
