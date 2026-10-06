"""Platform-wide model controls (FSD §4.6.6, cost-risk-governance spec R4).

Singleton row read on every model resolution through a short in-process TTL
cache (single backend task at Alpha → cache coherence is trivial; the S5+
multi-task world revisits this alongside the externalized event bus).
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import PlatformSettings
from app.services.bedrock_catalog import STATIC_BEDROCK_IDS, selectable_bedrock_catalog
from app.services.guardrails import EXT_PREFIX

logger = logging.getLogger("marshal.platform_settings")

CACHE_TTL_S = 60
_REGISTRY_IDS = list(STATIC_BEDROCK_IDS)  # deterministic boot-safe defaults


@dataclass(frozen=True)
class ModelControls:
    allowlist: list[str]
    temperature_min: float
    temperature_max: float
    top_p_min: float
    top_p_max: float
    max_tokens: int
    max_context: int
    rate_limits: dict = field(default_factory=dict)  # stored; enforced ≥S5
    cost: dict = field(default_factory=dict)  # platform budget feeds the cost dashboard
    # S17: ALL custom endpoints, enabled or not — the registry filters by
    # `enabled`; pricing deliberately does NOT (an in-flight call on a
    # just-disabled endpoint must still be priced, never "unpriced").
    custom_endpoints: list = field(default_factory=list)
    # S18: dark-launch switches
    feature_flags: dict = field(default_factory=dict)


_DEFAULTS = ModelControls(
    allowlist=list(_REGISTRY_IDS),
    temperature_min=0.0,
    temperature_max=1.0,
    top_p_min=0.0,
    top_p_max=1.0,
    max_tokens=8192,
    max_context=200_000,
)

_cache: tuple[float, ModelControls] | None = None


def _valid_model_ids(
    endpoints: list | None, bedrock_ids: list[str] | None = None
) -> list[str]:
    """Currently selectable Bedrock ids + enabled custom endpoint ids."""
    ids = list(bedrock_ids if bedrock_ids is not None else _REGISTRY_IDS)
    for endpoint in endpoints or []:
        if endpoint.get("enabled", True) and endpoint.get("slug"):
            ids.append(f"{EXT_PREFIX}{endpoint['slug']}")
    return ids


def _to_controls(
    row: PlatformSettings, bedrock_ids: list[str] | None = None
) -> ModelControls:
    bounds = row.param_bounds or {}
    temp = bounds.get("temperature", {})
    top_p = bounds.get("top_p", {})
    endpoints = list(getattr(row, "custom_model_endpoints", None) or [])
    valid_ids = _valid_model_ids(endpoints, bedrock_ids)
    stored_allowlist = list(row.model_allowlist or [])
    allowlist = [model for model in stored_allowlist if model in valid_ids]
    if not allowlist and get_settings().environment.strip().lower() != "cloud":
        # Local/test fixtures historically use placeholder ids. Cloud instead
        # fails closed when every persisted id becomes unavailable; it must not
        # silently broaden back to the four defaults.
        allowlist = list(_REGISTRY_IDS)
    return ModelControls(
        allowlist=allowlist,
        temperature_min=float(temp.get("min", 0.0)),
        temperature_max=float(temp.get("max", 1.0)),
        top_p_min=float(top_p.get("min", 0.0)),
        top_p_max=float(top_p.get("max", 1.0)),
        max_tokens=int(bounds.get("max_tokens", 8192)),
        max_context=int(bounds.get("max_context", 200_000)),
        rate_limits=row.rate_limits or {},
        cost=row.cost or {},
        custom_endpoints=endpoints,
        feature_flags=dict(getattr(row, "feature_flags", None) or {}),
    )


async def get_controls() -> ModelControls:
    """Cached settings intersected with the current selectable catalog.

    Missing settings still use boot defaults. In cloud, an existing row whose
    entire allowlist has disappeared resolves to an empty list so the model
    seam blocks instead of silently widening access.
    """
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL_S:
        return _cache[1]
    try:
        bedrock_ids = [
            model["id"] for model in await selectable_bedrock_catalog()
        ]
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            row = await db.get(PlatformSettings, 1)
        controls = _to_controls(row, bedrock_ids) if row else _DEFAULTS
    except Exception:  # noqa: BLE001 — resolution must not die on a settings read
        logger.exception("platform settings read failed; using defaults")
        controls = _DEFAULTS
    _cache = (now, controls)
    return controls


def invalidate_cache(*, clear_security_lkg: bool = False) -> None:
    """Invalidate freshness caches without discarding trusted security state."""
    global _cache, _security_cache, _security_last_known_good
    _cache = None
    _security_cache = None
    if clear_security_lkg:
        _security_last_known_good = None


# --- S14 security controls -------------------------------------------------
# Separate tiny cache: the MFA gate runs on every admin request, and the model
# controls cache carries a different (larger) payload. The LKG is separate so
# ordinary cache invalidation cannot turn a transient read failure into OFF.
SECURITY_DEFAULTS: dict = {
    "admin_mfa_required": False,  # ships OFF: enrol admins, then flip (S14-02)
    "pii_redaction": False,  # ships OFF: guardrail attached, then flip (S14-04)
    # Where masking applies when ON (S14-04 middle path): "all", or
    # "non_interactive" — every purpose except live chat, where the measured
    # +32%/41% TTFT cost is actually perceived by a reading human.
    "pii_redaction_scope": "all",
}
REDACTION_SCOPES = ("all", "non_interactive")
_security_cache: tuple[float, dict] | None = None
_security_last_known_good: dict | None = None


class SecuritySettingsUnavailable(RuntimeError):
    """No trustworthy security-control snapshot is available."""


def _validated_security_controls(stored: object) -> dict:
    """Validate persisted JSON before it can become a trusted snapshot."""
    if stored is None:
        stored = {}
    if not isinstance(stored, dict):
        raise TypeError("security settings must be an object")
    _validate_security(stored)
    merged = dict(SECURITY_DEFAULTS)
    merged.update({k: v for k, v in stored.items() if v is not None})
    return merged


def _has_mandatory_strong_posture(controls: dict) -> bool:
    """Whether a stale snapshot is strong enough for fail-closed operation."""
    return (
        controls.get("admin_mfa_required") is True
        and controls.get("pii_redaction") is True
        and controls.get("pii_redaction_scope") == "all"
    )


def _remember_security_controls(controls: dict, *, now: float | None = None) -> None:
    global _security_cache, _security_last_known_good
    snapshot = dict(controls)
    if get_settings().security_fail_closed and not _has_mandatory_strong_posture(
        snapshot
    ):
        # Before activation a successful DB read may return the configured OFF
        # posture, but cloud must never cache that weaker state: another task
        # can enable mandatory controls at any moment. A later read outage then
        # raises instead of reusing OFF for the cache window.
        _security_last_known_good = None
        _security_cache = None
        return
    _security_last_known_good = snapshot
    _security_cache = (time.monotonic() if now is None else now, snapshot)


async def get_security_controls() -> dict:
    """Return a validated security snapshot, preserving last-known-good state.

    Only a successful, schema-valid singleton read enters the cache. A stale
    snapshot survives transient read failures only when it carries the mandatory
    strong posture. Cloud fail-closed mode raises when no such snapshot exists;
    local/dev keeps the historical OFF defaults for bootstrap convenience
    without caching them as trustworthy.
    """
    global _security_cache
    now = time.monotonic()
    if _security_cache and now - _security_cache[0] < CACHE_TTL_S:
        return dict(_security_cache[1])
    try:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            row = await db.get(PlatformSettings, 1)
        if row is None:
            raise LookupError("platform settings singleton is missing")
        merged = _validated_security_controls(getattr(row, "security", None))
    except Exception as exc:  # noqa: BLE001 — posture depends on explicit mode
        logger.exception("security settings read failed")
        if (
            _security_last_known_good is not None
            and _has_mandatory_strong_posture(_security_last_known_good)
        ):
            logger.warning("using stale strong-posture security settings")
            _security_cache = (now, dict(_security_last_known_good))
            return dict(_security_last_known_good)
        if get_settings().security_fail_closed:
            raise SecuritySettingsUnavailable(
                "Security settings are temporarily unavailable"
            ) from exc
        logger.warning("no strong trusted security settings; using local fail-open defaults")
        return dict(SECURITY_DEFAULTS)
    _remember_security_controls(merged, now=now)
    return dict(merged)


async def redaction_active(purpose: str | None) -> bool:
    """Is masking on for THIS call? (S14-04 middle path.)

    scope=non_interactive exempts only live chat — the one purpose where a
    human perceives time-to-first-token. An unknown purpose (None) counts as
    non-interactive: when in doubt, mask.
    """
    controls = await get_security_controls()
    if not controls.get("pii_redaction"):
        return False
    if controls.get("pii_redaction_scope") == "non_interactive" and purpose == "chat":
        return False
    return True


def _validate_security(payload: dict) -> None:
    unknown = [k for k in payload if k not in SECURITY_DEFAULTS]
    if unknown:
        raise SettingsValidationError(
            f"Unknown security keys: {', '.join(sorted(unknown))}"
        )
    for key, value in payload.items():
        if key == "pii_redaction_scope":
            if value not in REDACTION_SCOPES:
                raise SettingsValidationError(
                    f"pii_redaction_scope must be one of: {', '.join(REDACTION_SCOPES)}"
                )
        elif not isinstance(value, bool):
            raise SettingsValidationError(f"{key} must be true or false")


class SettingsValidationError(Exception):
    """Invalid update payload — surfaces as 422."""


def _validate(
    allowlist: list[str],
    bounds: dict,
    rate_limits: dict,
    cost: dict,
    valid_ids: list[str] | None = None,
) -> None:
    known = valid_ids if valid_ids is not None else _REGISTRY_IDS
    unknown = [m for m in allowlist if m not in known]
    if unknown:
        raise SettingsValidationError(f"Unknown model ids: {', '.join(unknown)}")
    if not allowlist:
        raise SettingsValidationError("Allowlist cannot be empty — at least one model required")
    temp, top_p = bounds.get("temperature", {}), bounds.get("top_p", {})
    for name, b, lo, hi in (("temperature", temp, 0.0, 1.0), ("top_p", top_p, 0.0, 1.0)):
        bmin, bmax = float(b.get("min", lo)), float(b.get("max", hi))
        if not (lo <= bmin <= bmax <= hi):
            raise SettingsValidationError(f"{name} bounds must satisfy {lo} ≤ min ≤ max ≤ {hi}")
    if not (256 <= int(bounds.get("max_tokens", 8192)) <= 200_000):
        raise SettingsValidationError("max_tokens must be between 256 and 200000")
    for key in ("per_user_rpm", "per_project_rpm", "platform_rpm"):
        if key in rate_limits and int(rate_limits[key]) <= 0:
            raise SettingsValidationError(f"{key} must be positive")
    budget = cost.get("platform_budget_usd")
    if budget is not None and float(budget) < 0:
        raise SettingsValidationError("platform_budget_usd cannot be negative")


def _validate_codegen(codegen: dict) -> dict:
    """Validate the codegen block and return it with a normalized provider.

    `kiro` is accepted as a legacy alias for `runner` and stored as `runner`.
    """
    from app.services.codegen.provider import PROVIDER_NAMES, normalize_provider_name

    provider = codegen.get("provider")
    if provider is None:
        return codegen
    if not isinstance(provider, str):
        raise SettingsValidationError("codegen provider must be 'internal' or 'runner'")
    normalized = normalize_provider_name(provider)
    if normalized not in PROVIDER_NAMES:
        raise SettingsValidationError("codegen provider must be 'internal' or 'runner'")
    return {**codegen, "provider": normalized}


def _validate_deployment_policies(payload: dict) -> None:
    from app.services.policies import DEFAULTS, validate_policies

    unknown = [k for k in payload if k not in DEFAULTS]
    if unknown:
        raise SettingsValidationError(f"Unknown policy keys: {', '.join(sorted(unknown))}")
    # Validate the EFFECTIVE merged policy so partial payloads cannot smuggle
    # an inconsistent combination past per-key checks (e.g. default_ttl > max).
    merged = {**DEFAULTS, **{k: v for k, v in payload.items() if v is not None}}
    try:
        validate_policies(merged)
    except ValueError as exc:
        raise SettingsValidationError(str(exc)) from exc


async def update_controls(
    db: AsyncSession,
    *,
    model_allowlist: list[str],
    param_bounds: dict,
    rate_limits: dict,
    cost: dict,
    codegen: dict | None = None,
    deployment_policies: dict | None = None,
    security: dict | None = None,
    feature_flags: dict | None = None,
    updated_by,
) -> PlatformSettings:
    row = await db.get(PlatformSettings, 1)
    # Allowlist domain includes the row's ENABLED custom endpoints (S17):
    # admins can allowlist `ext/<slug>` ids exactly like Bedrock ids.
    endpoints = list(getattr(row, "custom_model_endpoints", None) or []) if row else []
    bedrock_ids = [model["id"] for model in await selectable_bedrock_catalog()]
    _validate(
        model_allowlist,
        param_bounds,
        rate_limits,
        cost,
        valid_ids=_valid_model_ids(endpoints, bedrock_ids),
    )
    if codegen is not None:
        codegen = _validate_codegen(codegen)
    if deployment_policies is not None:
        _validate_deployment_policies(deployment_policies)
        from app.services.policies import DEFAULTS as DEPLOYMENT_POLICY_DEFAULTS

        # Persist a complete snapshot. Cloud admission treats missing keys as
        # untrusted/paused; normalization makes a successful admin save an
        # explicit policy decision rather than another fallback to code defaults.
        deployment_policies = {
            **DEPLOYMENT_POLICY_DEFAULTS,
            **{
                key: value
                for key, value in deployment_policies.items()
                if value is not None
            },
        }
    if security is not None:
        _validate_security(security)
    if feature_flags is not None:
        for key, value in feature_flags.items():
            if not isinstance(key, str) or not isinstance(value, bool):
                raise SettingsValidationError(
                    "feature_flags must map string flag names to booleans"
                )
    if row is None:  # first boot on a pre-seed database
        row = PlatformSettings(id=1)
        db.add(row)
    row.model_allowlist = model_allowlist
    row.param_bounds = param_bounds
    row.rate_limits = rate_limits
    row.cost = cost
    if codegen is not None:
        row.codegen = codegen
    if deployment_policies is not None:
        row.deployment_policies = deployment_policies
    if security is not None:
        row.security = {**(row.security or {}), **security}
    if feature_flags is not None:
        # Merge, not replace: a PUT that omits a flag must not silently kill it.
        row.feature_flags = {**(getattr(row, "feature_flags", None) or {}), **feature_flags}
    # The successful write itself is trustworthy. Validate before commit, then
    # seed the LKG after commit so an immediate read outage keeps this posture.
    security_snapshot = _validated_security_controls(
        getattr(row, "security", None)
    )
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    invalidate_cache()
    _remember_security_controls(security_snapshot)
    return row


def clamp_temperature(value: float | None, controls: ModelControls) -> float | None:
    if value is None:
        return None
    return min(max(value, controls.temperature_min), controls.temperature_max)


def clamp_top_p(value: float | None, controls: ModelControls) -> float | None:
    if value is None:
        return None
    return min(max(value, controls.top_p_min), controls.top_p_max)
