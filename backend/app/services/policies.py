"""Deployment policies (platform-scale-policies spec R1, §4.6.6 family).

Hard bounds on Enclave usage enforced at deploy pre-flight — cheapest checks
first, BEFORE the risk gate. Where ISB lease templates cannot express a policy
(per-lease budget overrides), the platform enforces its own bound and the gap
feeds the OQ-7 ADR.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Deployment, PlatformSettings

logger = logging.getLogger("marshal.policies")

DEPLOY_REGION = "us-east-1"  # as-built single-region; the allowlist is groundwork

DEFAULTS = {
    # Operator kill switch. Cloud also forces this effective value to true when
    # no trustworthy persisted policy snapshot can be read.
    "admissions_paused": False,
    "max_concurrent_per_user": 3,
    "max_concurrent_platform": 10,
    # Preserve this many ISB accounts in Available state after each admission.
    "provider_capacity_buffer": 1,
    "allowed_regions": [DEPLOY_REGION],
    "default_ttl_hours": 72,
    "max_ttl_hours": 168,
    "per_deployment_budget_usd": 200,
    # B20 R0.5 (relocated from rate_limits): standing blast-radius bound —
    # total distinct provider-backed project reservations one user may hold.
    # 0 disables the check.
    "max_active_deployments_per_user": 3,
    # B20 R1.2 deployment modes. testbed_gate_mode: advisory = score, store,
    # route review, DISPLAY — never block; off = skip scoring at deploy.
    # Full-governance deploys always enforce (the name is the contract).
    "testbed_enabled": True,
    "testbed_gate_mode": "advisory",
    "testbed_session_hours": 4,  # requested; chained task-role sessions cap at 1h
    "default_mode": "full_governance",  # deploy-form preselect
    # B21: default = new builds keyed unless explicit PUBLIC intent; always =
    # explicit-public/legacy-unknown payloads refused before risk and leasing.
    "require_endpoint_auth": "default",
}

NON_TERMINAL = ("pending", "pre_flight", "leasing", "deploying", "updating", "active")


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DeploymentPolicies:
    admissions_paused: bool
    max_concurrent_per_user: int
    max_concurrent_platform: int
    provider_capacity_buffer: int
    allowed_regions: list[str]
    default_ttl_hours: int
    max_ttl_hours: int
    per_deployment_budget_usd: float
    max_active_deployments_per_user: int
    testbed_enabled: bool
    testbed_gate_mode: str
    testbed_session_hours: int
    default_mode: str
    require_endpoint_auth: str


async def resolve_deployment_policies(db: AsyncSession) -> DeploymentPolicies:
    cloud = get_settings().environment.strip().lower() == "cloud"
    trusted_snapshot = True
    try:
        row = await db.get(PlatformSettings, 1)
        stored_value = getattr(row, "deployment_policies", None) if row else None
        if not isinstance(stored_value, dict) or not stored_value:
            trusted_snapshot = False
            stored = {}
        else:
            stored = stored_value
            if cloud and not set(DEFAULTS).issubset(stored):
                trusted_snapshot = False
        legacy_rate_limits = (row.rate_limits or {}) if row else {}
    except Exception:  # noqa: BLE001 — cloud turns read trouble into pause
        logger.exception("deployment policy snapshot unavailable")
        trusted_snapshot = False
        stored, legacy_rate_limits = {}, {}
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if v is not None}}
    if cloud and not trusted_snapshot:
        merged["admissions_paused"] = True
        logger.error("deployment admissions forced paused: no trusted policy snapshot")
    # B20 R0.5: honor the legacy rate_limits location until an admin save
    # normalizes — existing customized quotas must keep working unchanged.
    if (
        "max_active_deployments_per_user" not in stored
        and legacy_rate_limits.get("max_active_deployments_per_user") is not None
    ):
        merged["max_active_deployments_per_user"] = legacy_rate_limits[
            "max_active_deployments_per_user"
        ]
    try:
        validate_policies(merged)
    except (TypeError, ValueError):
        logger.exception("stored deployment policy snapshot is invalid")
        merged = dict(DEFAULTS)
        if cloud:
            merged["admissions_paused"] = True
    return DeploymentPolicies(
        admissions_paused=bool(merged["admissions_paused"]),
        max_concurrent_per_user=int(merged["max_concurrent_per_user"]),
        max_concurrent_platform=int(merged["max_concurrent_platform"]),
        provider_capacity_buffer=int(merged["provider_capacity_buffer"]),
        allowed_regions=list(merged["allowed_regions"]),
        default_ttl_hours=int(merged["default_ttl_hours"]),
        max_ttl_hours=int(merged["max_ttl_hours"]),
        per_deployment_budget_usd=float(merged["per_deployment_budget_usd"]),
        max_active_deployments_per_user=int(merged["max_active_deployments_per_user"]),
        testbed_enabled=bool(merged["testbed_enabled"]),
        testbed_gate_mode=(
            merged["testbed_gate_mode"]
            if merged["testbed_gate_mode"] in ("advisory", "off")
            else "advisory"
        ),
        testbed_session_hours=min(12, max(1, int(merged["testbed_session_hours"]))),
        default_mode=(
            merged["default_mode"]
            if merged["default_mode"] in ("full_governance", "testbed")
            else "full_governance"
        ),
        require_endpoint_auth=(
            merged["require_endpoint_auth"]
            if merged["require_endpoint_auth"] in ("default", "always")
            else "default"
        ),
    )


def validate_policies(payload: dict) -> None:
    if "admissions_paused" in payload and not isinstance(
        payload["admissions_paused"], bool
    ):
        raise ValueError("admissions_paused must be a boolean")
    buffer = payload.get("provider_capacity_buffer")
    if buffer is not None and (
        not isinstance(buffer, int) or isinstance(buffer, bool) or buffer < 0
    ):
        raise ValueError("provider_capacity_buffer must be a non-negative integer")
    for key in (
        "max_concurrent_per_user",
        "max_concurrent_platform",
        "default_ttl_hours",
        "max_ttl_hours",
    ):
        if key in payload and (
            not isinstance(payload[key], int)
            or isinstance(payload[key], bool)
            or payload[key] < 1
        ):
            raise ValueError(f"{key} must be a positive integer")
    quota = payload.get("max_active_deployments_per_user")
    if quota is not None and (
        not isinstance(quota, int) or isinstance(quota, bool) or quota < 0
    ):
        raise ValueError(
            "max_active_deployments_per_user must be a non-negative integer (0 disables)"
        )
    # B20 R1.2 deployment-mode policy keys
    if "testbed_enabled" in payload and not isinstance(payload["testbed_enabled"], bool):
        raise ValueError("testbed_enabled must be a boolean")
    gate = payload.get("testbed_gate_mode")
    if gate is not None and gate not in ("advisory", "off"):
        raise ValueError("testbed_gate_mode must be 'advisory' or 'off'")
    hours = payload.get("testbed_session_hours")
    if hours is not None and (not isinstance(hours, int) or not 1 <= hours <= 12):
        raise ValueError("testbed_session_hours must be an integer between 1 and 12")
    mode = payload.get("default_mode")
    if mode is not None and mode not in ("full_governance", "testbed"):
        raise ValueError("default_mode must be 'full_governance' or 'testbed'")
    endpoint_auth = payload.get("require_endpoint_auth")
    if endpoint_auth is not None and endpoint_auth not in ("default", "always"):
        raise ValueError("require_endpoint_auth must be 'default' or 'always'")
    if payload.get("default_ttl_hours", 1) > payload.get("max_ttl_hours", 10**6):
        raise ValueError("default_ttl_hours cannot exceed max_ttl_hours")
    regions = payload.get("allowed_regions")
    if regions is not None and (not isinstance(regions, list) or not regions):
        raise ValueError("allowed_regions must be a non-empty list")
    budget = payload.get("per_deployment_budget_usd")
    if budget is not None and float(budget) <= 0:
        raise ValueError("per_deployment_budget_usd must be positive")


def _provider_reservations(user_id: uuid.UUID | None = None):
    """Rows currently consuming or reserving physical provider capacity.

    Every unterminated lease is a physical reservation, including multiple
    leases for one project. Lease-free deployment attempts reserve once per
    project until a lease row becomes the authoritative physical reservation.
    """
    from sqlalchemy import union_all

    from app.models import Lease

    unterminated_lease = or_(
        Lease.status != "terminated", Lease.terminated_at.is_(None)
    )
    lease_reservations = select(Lease.id.label("reservation_id")).where(
        unterminated_lease
    )
    project_has_lease = (
        select(Lease.id)
        .where(
            Lease.project_id == Deployment.project_id,
            unterminated_lease,
        )
        .exists()
    )
    deployment_reservations = (
        select(Deployment.project_id.label("reservation_id"))
        .where(
            Deployment.status.in_((*NON_TERMINAL, "tearing_down")),
            ~project_has_lease,
        )
        .distinct()
    )
    if user_id is not None:
        deployment_reservations = deployment_reservations.where(
            Deployment.user_id == user_id
        )
        lease_reservations = lease_reservations.where(Lease.user_id == user_id)
    return union_all(lease_reservations, deployment_reservations).subquery(
        "provider_reservations"
    )


async def count_provider_reservations(
    db: AsyncSession, user_id: uuid.UUID | None = None
) -> int:
    """Count physical leases plus lease-free deployment reservations."""
    reservations = _provider_reservations(user_id)
    count = (
        await db.execute(select(func.count()).select_from(reservations))
    ).scalar_one()
    return int(count)


async def enforce_deploy_capacity(
    db: AsyncSession,
    user_id: uuid.UUID,
    is_update: bool,
    *,
    policies: DeploymentPolicies | None = None,
) -> DeploymentPolicies:
    """Check mutable deployment capacity, exempting in-place updates.

    The API uses this in its cheap pre-risk preflight. Final admission repeats
    it under the transaction-scoped deployment lock, making these counts
    authoritative for the pending-row commit.
    """
    policies = policies or await resolve_deployment_policies(db)
    if policies.admissions_paused:
        raise PolicyViolation(
            "admissions_paused",
            "Deployment admissions are paused by platform policy or because "
            "the cloud policy snapshot is unavailable.",
        )
    if is_update:
        return policies
    user_count = await count_provider_reservations(db, user_id)
    if user_count >= policies.max_concurrent_per_user:
        raise PolicyViolation(
            "policy_concurrency_user",
            f"You already have {user_count} live/in-flight deployments "
            f"(limit {policies.max_concurrent_per_user}) — tear one down or ask an "
            "admin to raise the limit under Model Controls.",
        )
    platform_count = await count_provider_reservations(db)
    if platform_count >= policies.max_concurrent_platform:
        raise PolicyViolation(
            "policy_concurrency_platform",
            f"The platform is at its concurrent-deployment limit "
            f"({policies.max_concurrent_platform}) — try again shortly.",
        )
    return policies


async def enforce_deploy_preflight(
    db: AsyncSession,
    user_id: uuid.UUID,
    is_update: bool,
    *,
    endpoint_auth: dict | None = None,
) -> DeploymentPolicies:
    """Cheap policy checks before risk/model spend or lease creation.

    B21 `always` accepts only a build manifest proven keyed by the new
    contract (or an old build with the explicit B13 key phrase). Public,
    contradictory, sample and legacy no-signal payloads must be rebuilt.
    """
    policies = await resolve_deployment_policies(db)
    if DEPLOY_REGION not in policies.allowed_regions:
        raise PolicyViolation(
            "policy_region",
            f"Deployments to {DEPLOY_REGION} are disabled by platform policy",
        )
    if policies.require_endpoint_auth == "always":
        trusted_keyed = (
            endpoint_auth is not None
            and endpoint_auth.get("mode") == "key_required"
            and endpoint_auth.get("source") not in ("conflict", "legacy_unknown")
        )
        if not trusted_keyed:
            if endpoint_auth and endpoint_auth.get("source") == "explicit_public":
                detail = (
                    "This installation requires endpoint authentication; remove "
                    "the explicit PUBLIC opt-out and rebuild"
                )
            else:
                detail = (
                    "This installation requires endpoint authentication; this "
                    "legacy or sample payload has no trustworthy keyed-build "
                    "contract — rebuild it before deploying"
                )
            raise PolicyViolation("policy_endpoint_auth", detail)
    return await enforce_deploy_capacity(
        db, user_id, is_update, policies=policies
    )
