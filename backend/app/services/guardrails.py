"""Template model-guardrail resolution — the single enforcement seam (template spec R4).

Every Bedrock-calling code path (chat, spec generation, title suggestion) resolves
its models through here. Enforcement is code-level: the server picks the model id;
nothing a user types can widen the allowlist.
"""

from dataclasses import dataclass

from app.core.config import get_settings
from app.models import Template
from app.services.bedrock_catalog import (
    STATIC_BEDROCK_FALLBACK,
    get_bedrock_catalog,
)

# S17 provider-backed registry. The legacy aliases remain the deterministic,
# boot-safe four-model view for old imports/tests; runtime resolution uses the
# live governed catalog returned by `model_registry()` below.
EXT_PREFIX = "ext/"

BEDROCK_REGISTRY: list[dict] = [dict(model) for model in STATIC_BEDROCK_FALLBACK]
MODEL_REGISTRY = BEDROCK_REGISTRY


def registry_entry_for_endpoint(endpoint: dict) -> dict:
    """Registry entry for one enabled custom endpoint (S17-01)."""
    return {
        "id": f"{EXT_PREFIX}{endpoint['slug']}",
        "base_model_id": endpoint.get("model") or endpoint["slug"],
        "label": endpoint.get("label") or endpoint["slug"],
        "tier": endpoint.get("tier", "standard"),
        "provider": "openai_compat",
        "provider_name": endpoint.get("provider") or "External",
        "source": "external",
        "status": "ACTIVE",
        "availability": "configured",
        "selectable": True,
        "disabled_reason": None,
        "converse_supported": True,
        "supports_streaming": True,
        "input_modalities": ["TEXT"],
        "output_modalities": ["TEXT"],
        "inference_type": "external_endpoint",
        "inference_profiles": [],
        "routing_scope": "external",
        "access_status": "configured",
        "pricing": {
            "status": "admin_configured",
            "input_usd_per_1k": endpoint.get("usd_per_1k_input"),
            "output_usd_per_1k": endpoint.get("usd_per_1k_output"),
        },
        "catalog_source": "platform_settings",
        "catalog_stale": False,
        "catalog_error": None,
    }


async def model_registry() -> list[dict]:
    """Live Bedrock discovery (including disabled rows) + enabled endpoints.

    A catalog row is visible even when it is not selectable, so admins can see
    why an active regional model was excluded. Runtime callers must intersect
    with the `selectable` flag; persisted allowlists never broaden merely
    because AWS publishes a new model.
    """
    entries = await get_bedrock_catalog()
    try:
        from app.services.platform_settings import get_controls

        controls = await get_controls()
        for endpoint in controls.custom_endpoints:
            if endpoint.get("enabled", True):
                entries.append(registry_entry_for_endpoint(endpoint))
    except Exception:  # noqa: BLE001 — losing external routes is fail-closed
        import logging

        logging.getLogger("marshal.guardrails").warning(
            "custom endpoint registry unavailable; resolving Bedrock-only"
        )
    return entries


class GuardrailViolation(Exception):
    """Raised when a template's guardrails cannot be satisfied (surfaces as 422)."""


@dataclass(frozen=True)
class ResolvedModels:
    chat: str
    requirements: str
    design: str
    tasks: str
    fast: str
    codegen: str  # S17-05: governed purpose — resolves Bedrock-only
    max_tokens: int  # chat budget
    max_tokens_generation: int  # per-document generation budget
    template_name: str | None = None
    # S18 studio rail: the selectable domain + template temperature rails, so
    # requested-vs-effective rendering shares THIS resolution, never a parallel
    # client-side calculation (studio spec R3.2).
    allowed: tuple[str, ...] = ()
    temperature_bounds: tuple[float, float] | None = None


def _fallback(
    preferred: str,
    allowed: list[str],
    registry: list[dict],
    *,
    sources: set[str] | None = None,
) -> str:
    """Preferred model if allowed, else tier-mapped closest, else first allowed.

    `sources` restricts candidates by registry source — the S17-05 purpose pin:
    governed purposes (risk scoring, codegen) pass {"aws"} so an allowlist that
    contains external models can never route THOSE calls off Bedrock. Widening
    a purpose to external models means changing its `sources` here — one
    documented line, not an archaeology dig.
    """
    if sources is not None:
        source_ok = {m["id"] for m in registry if m.get("source", "aws") in sources}
        allowed = [m for m in allowed if m in source_ok]
        if not allowed:
            # The pin outranks the allowlist: governed purposes fall back to
            # the platform default Bedrock models even when an admin allowlists
            # only external models (recorded stance, custom-model-endpoints R5.4).
            allowed = [m["id"] for m in registry if m.get("source", "aws") in sources]
    if preferred in allowed:
        return preferred
    preferred_tier = next(
        (m["tier"] for m in registry if m["id"] == preferred), "standard"
    )
    tier_order = {
        "advanced": ["advanced", "standard", "fast"],
        "standard": ["standard", "advanced", "fast"],
        "fast": ["fast", "standard", "advanced"],
    }[preferred_tier]
    for tier in tier_order:
        for model in registry:
            if model["tier"] == tier and model["id"] in allowed:
                return model["id"]
    return allowed[0]


async def resolve_models(template: Template | None) -> ResolvedModels:
    """Effective models = platform allowlist ∩ template allowlist (S4-04 R4.3).

    The platform allowlist is the OUTER bound: an admin narrowing it at
    /admin/models constrains every session — templated or not — on the next
    resolution (≤60s cache). GuardrailViolation names whichever layer emptied
    the intersection so the 422 is actionable.
    """
    from app.services.platform_settings import get_controls

    settings = get_settings()
    controls = await get_controls()
    discovered_registry = await model_registry()
    registry = [
        model for model in discovered_registry if model.get("selectable", True)
    ]
    registry_ids = [m["id"] for m in registry]
    platform_allowed = [m for m in controls.allowlist if m in registry_ids]
    gen_tokens = getattr(settings, "bedrock_max_tokens_generation", 8192)

    defaults = ResolvedModels(
        chat=settings.bedrock_model_chat,
        requirements=settings.bedrock_model_spec,
        design=settings.bedrock_model_design,
        tasks=settings.bedrock_model_spec,
        fast=settings.bedrock_model_fast,
        codegen=settings.bedrock_model_spec,
        max_tokens=min(settings.bedrock_max_tokens, controls.max_tokens),
        max_tokens_generation=min(gen_tokens, controls.max_tokens),
    )

    temp_bounds: tuple[float, float] | None = None
    if template is None:
        allowed = platform_allowed
        template_cap = None
        template_name = None
    else:
        model_rails = (template.guardrails or {}).get("model", {})
        allowed_raw = model_rails.get("allowed_models") or []
        template_allowed = [m for m in allowed_raw if m in registry_ids]
        if allowed_raw and not template_allowed:
            raise GuardrailViolation(
                f"Template '{template.name}' allows no model known to this platform. "
                f"Known models: {', '.join(registry_ids)}"
            )
        if not template_allowed:  # no model guardrails configured → template imposes nothing
            template_allowed = list(registry_ids)
        allowed = [m for m in template_allowed if m in platform_allowed]
        if not allowed:
            raise GuardrailViolation(
                f"No usable model: the platform allowlist and template "
                f"'{template.name}' have no models in common. An admin can widen "
                "the platform allowlist at Admin → Model Controls, or the template's "
                "allowed models."
            )
        template_cap = model_rails.get("max_tokens")
        template_name = template.name
        rail_temp = model_rails.get("temperature") or {}
        if "min" in rail_temp or "max" in rail_temp:
            temp_bounds = (
                float(rail_temp.get("min", 0.0)),
                float(rail_temp.get("max", 1.0)),
            )

    if not allowed:  # platform allowlist emptied entirely (guarded at update, belt+braces)
        raise GuardrailViolation(
            "The platform model allowlist is empty. An admin must allow at least "
            "one model at Admin → Model Controls."
        )

    max_tokens = min(defaults.max_tokens, int(template_cap or defaults.max_tokens))
    max_tokens_generation = min(
        defaults.max_tokens_generation, int(template_cap or defaults.max_tokens_generation)
    )
    return ResolvedModels(
        chat=_fallback(defaults.chat, allowed, registry),
        requirements=_fallback(defaults.requirements, allowed, registry),
        design=_fallback(defaults.design, allowed, registry),
        tasks=_fallback(defaults.tasks, allowed, registry),
        # S17-05 purpose pin: risk scoring/titling/classification (fast) and
        # codegen never route to external endpoints, whatever the allowlist says.
        fast=_fallback(defaults.fast, allowed, registry, sources={"aws"}),
        codegen=_fallback(defaults.codegen, allowed, registry, sources={"aws"}),
        max_tokens=max_tokens,
        max_tokens_generation=max_tokens_generation,
        template_name=template_name,
        allowed=tuple(allowed),
        temperature_bounds=temp_bounds,
    )


@dataclass(frozen=True)
class SessionCallPlan:
    """What the next chat call will actually use, and why (studio spec R3.2).

    Built by `resolve_session_call` — the ONE place session-requested values
    meet the platform/template clamps. chat's send_message consumes
    model_id/temperature/max_tokens; the effective-params endpoint serializes
    requested/effective/clamped_by. Display cannot drift from enforcement
    because both read this object.
    """

    model_id: str
    temperature: float | None
    max_tokens: int
    requested: dict
    effective: dict
    clamped_by: dict  # field -> "platform" | "template '<name>'" | None


async def resolve_session_call(
    template: Template | None, session_params: dict | None
) -> SessionCallPlan:
    """Apply session-requested overrides through the same rails every call uses.

    Order per field: template narrows first (it can only narrow), then platform
    bounds (the outer ceiling, S4-04). A requested model outside the resolved
    allowlist falls back to the resolved chat default — recorded in clamped_by
    so the rail can say so instead of silently ignoring the pick.
    """
    from app.services.platform_settings import get_controls

    models = await resolve_models(template)
    controls = await get_controls()
    params = session_params or {}
    clamped_by: dict[str, str | None] = {"model_id": None, "temperature": None, "max_tokens": None}
    template_label = f"template '{models.template_name}'" if models.template_name else "template"

    # Model: requested must sit in the resolved (platform ∩ template) domain.
    requested_model = params.get("model_id")
    if requested_model and requested_model in models.allowed:
        model_id = requested_model
    else:
        model_id = models.chat
        if requested_model:
            clamped_by["model_id"] = (
                template_label
                if models.template_name and requested_model in controls.allowlist
                else "platform"
            )

    # Temperature: None means "platform default" (we send nothing to the model).
    requested_temp = params.get("temperature")
    temperature = requested_temp
    if temperature is not None:
        temperature = float(temperature)
        if models.temperature_bounds is not None:
            lo, hi = models.temperature_bounds
            narrowed = min(max(temperature, lo), hi)
            if narrowed != temperature:
                clamped_by["temperature"] = template_label
            temperature = narrowed
        platform_clamped = min(
            max(temperature, controls.temperature_min), controls.temperature_max
        )
        if platform_clamped != temperature:
            clamped_by["temperature"] = "platform"
        temperature = platform_clamped

    # Max tokens: models.max_tokens is already template ∩ platform.
    requested_max = params.get("max_tokens")
    if requested_max:
        max_tokens = min(int(requested_max), models.max_tokens)
        if max_tokens != int(requested_max):
            clamped_by["max_tokens"] = (
                template_label
                if models.template_name and models.max_tokens < controls.max_tokens
                else "platform"
            )
    else:
        max_tokens = models.max_tokens

    return SessionCallPlan(
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        requested={
            "model_id": requested_model,
            "temperature": requested_temp,
            "max_tokens": requested_max,
        },
        effective={
            "model_id": model_id,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        clamped_by=clamped_by,
    )


def template_prompt_block(template: Template | None) -> str:
    """Extra system-prompt context when a template governs the session (R4.3/R4.4)."""
    if template is None:
        return ""
    scaffolding = template.scaffolding or {}
    required = scaffolding.get("required_spec_sections") or []
    lines = [
        f'\nThis session is governed by the "{template.name}" template.',
        "If the user asks to use a model or approach the template forbids, explain the "
        "constraint politely and suggest the closest allowed alternative.",
    ]
    if template.description:
        lines.append(f"Template intent: {template.description}")
    if required:
        lines.append(
            "The requirements document MUST include these sections/topics: "
            + ", ".join(required)
        )
    return "\n".join(lines)
