"""Governed Amazon Bedrock catalog discovery for Marshal's Converse contract.

The Bedrock control-plane catalog is broader than Marshal's runtime contract:
entries can be embeddings, rerankers, provisioned-only aliases, or models whose
supported API is not Converse.  This service discovers the live regional view,
then intersects it with a reviewed compatibility overlay.  Discovery can only
narrow or disable entries; it never widens the platform allowlist by itself.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import UTC, datetime
from typing import Any

import boto3

from app.core.config import get_settings
from app.services.pricing import pricing_metadata

logger = logging.getLogger("marshal.bedrock_catalog")

CATALOG_TTL_S = 6 * 60 * 60

# Verified against the live us-east-1 ListFoundationModels inventory and AWS's
# API compatibility documentation on 31 Aug 2026.  A newly discovered model is
# visible but disabled until it is deliberately added here; catalog growth must
# never silently broaden Marshal's runtime contract.
CONVERSE_TEXT_MODEL_IDS = frozenset(
    {
        "amazon.nova-2-lite-v1:0",
        "amazon.nova-lite-v1:0",
        "amazon.nova-micro-v1:0",
        "amazon.nova-pro-v1:0",
        "anthropic.claude-fable-5",
        "anthropic.claude-fable-5-1",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-opus-4-5-20251101-v1:0",
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-opus-4-8",
        "anthropic.claude-opus-5",
        "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-sonnet-5",
        "deepseek.r1-v1:0",
        "deepseek.v3.2",
        "google.gemma-3-12b-it",
        "google.gemma-3-27b-it",
        "google.gemma-3-4b-it",
        "meta.llama3-1-70b-instruct-v1:0",
        "meta.llama3-1-8b-instruct-v1:0",
        "meta.llama3-3-70b-instruct-v1:0",
        "meta.llama3-70b-instruct-v1:0",
        "meta.llama3-8b-instruct-v1:0",
        "meta.llama4-maverick-17b-instruct-v1:0",
        "meta.llama4-scout-17b-instruct-v1:0",
        "minimax.minimax-m2",
        "minimax.minimax-m2.1",
        "minimax.minimax-m2.5",
        "mistral.devstral-2-123b",
        "mistral.magistral-small-2509",
        "mistral.ministral-3-14b-instruct",
        "mistral.ministral-3-3b-instruct",
        "mistral.ministral-3-8b-instruct",
        "mistral.mistral-7b-instruct-v0:2",
        "mistral.mistral-large-2402-v1:0",
        "mistral.mistral-large-3-675b-instruct",
        "mistral.mistral-small-2402-v1:0",
        "mistral.mixtral-8x7b-instruct-v0:1",
        "mistral.pixtral-large-2502-v1:0",
        "mistral.voxtral-mini-3b-2507",
        "mistral.voxtral-small-24b-2507",
        "moonshot.kimi-k2-thinking",
        "moonshotai.kimi-k2.5",
        "nvidia.nemotron-nano-12b-v2",
        "nvidia.nemotron-nano-3-30b",
        "nvidia.nemotron-nano-9b-v2",
        "nvidia.nemotron-super-3-120b",
        "openai.gpt-5.6-luna",
        "openai.gpt-5.6-sol",
        "openai.gpt-5.6-terra",
        "openai.gpt-oss-120b-1:0",
        "openai.gpt-oss-20b-1:0",
        "openai.gpt-oss-safeguard-120b",
        "openai.gpt-oss-safeguard-20b",
        "qwen.qwen3-32b-v1:0",
        "qwen.qwen3-coder-30b-a3b-v1:0",
        "qwen.qwen3-coder-next",
        "qwen.qwen3-next-80b-a3b",
        "qwen.qwen3-vl-235b-a22b",
        "writer.palmyra-vision-7b",
        "writer.palmyra-x4-v1:0",
        "writer.palmyra-x5-v1:0",
        "xai.grok-4.6",
        "zai.glm-4.7",
        "zai.glm-4.7-flash",
        "zai.glm-5",
    }
)

NON_CONVERSE_REASONS = {
    "cohere.rerank-v3-5:0": "Rerank API contract; AWS does not support Converse",
    "twelvelabs.pegasus-1-2-v1:0": (
        "video analysis contract; AWS does not support Converse"
    ),
}

# Boot-safe LKG when the control plane is unavailable.  These are the four
# models already deployed and proven by Marshal before dynamic discovery.
_STATIC_MODELS = (
    (
        "us.anthropic.claude-sonnet-5",
        "anthropic.claude-sonnet-5",
        "Claude Sonnet 5",
        "standard",
    ),
    (
        "us.anthropic.claude-sonnet-4-6",
        "anthropic.claude-sonnet-4-6",
        "Claude Sonnet 4.6",
        "standard",
    ),
    (
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "Claude Haiku 4.5",
        "fast",
    ),
    (
        "us.anthropic.claude-opus-4-8",
        "anthropic.claude-opus-4-8",
        "Claude Opus 4.8",
        "advanced",
    ),
)


def _tier(model_id: str, label: str) -> str:
    """Stable coarse capability tier for routing and the risk rubric."""
    text = f"{model_id} {label}".lower()
    advanced = (
        "opus",
        "nova-pro",
        "large",
        "maverick",
        "scout",
        "70b",
        "80b",
        "120b",
        "123b",
        "235b",
        "675b",
        "terra",
        "sol",
        "thinking",
        "glm-5",
    )
    fast = (
        "haiku",
        "micro",
        "lite",
        "flash",
        "luna",
        "3b",
        "4b",
        "7b",
        "8b",
        "9b",
        "12b",
        "14b",
        "20b",
    )
    if any(marker in text for marker in advanced):
        return "advanced"
    if any(marker in text for marker in fast):
        return "fast"
    return "standard"


def _profile_priority(profile_id: str) -> tuple[int, str]:
    if profile_id.startswith("us."):
        return (0, profile_id)
    if profile_id.startswith("global."):
        return (1, profile_id)
    return (2, profile_id)


def _base_model_id_from_arn(arn: str) -> str | None:
    marker = "foundation-model/"
    if marker not in arn:
        return None
    return arn.split(marker, 1)[1]


def _fallback_entries() -> list[dict[str, Any]]:
    updated_at = datetime.now(UTC).isoformat()
    return [
        {
            "id": invocation_id,
            "base_model_id": base_model_id,
            "label": label,
            "tier": tier,
            "provider": "bedrock",
            "provider_name": "Anthropic",
            "source": "aws",
            "status": "ACTIVE",
            "availability": "fallback_lkg",
            "selectable": True,
            "disabled_reason": None,
            "converse_supported": True,
            "supports_streaming": True,
            "input_modalities": ["TEXT"],
            "output_modalities": ["TEXT"],
            "inference_type": "inference_profile",
            "inference_profiles": [invocation_id],
            "routing_scope": "us",
            "access_status": "previously_proven",
            "pricing": pricing_metadata(invocation_id),
            "catalog_source": "static_lkg",
            "catalog_stale": False,
            "catalog_error": None,
            "catalog_updated_at": updated_at,
        }
        for invocation_id, base_model_id, label, tier in _STATIC_MODELS
    ]


STATIC_BEDROCK_FALLBACK = _fallback_entries()
STATIC_BEDROCK_IDS = tuple(entry["id"] for entry in STATIC_BEDROCK_FALLBACK)

_catalog_lkg: list[dict[str, Any]] | None = None
_catalog_cached_at = 0.0
_refresh_lock = asyncio.Lock()


def _list_profiles(client) -> list[dict]:
    profiles: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "typeEquals": "SYSTEM_DEFINED",
            "maxResults": 1000,
        }
        if token:
            kwargs["nextToken"] = token
        response = client.list_inference_profiles(**kwargs)
        profiles.extend(response.get("inferenceProfileSummaries", []))
        token = response.get("nextToken")
        if not token:
            return profiles


def _discover_sync() -> list[dict[str, Any]]:
    settings = get_settings()
    client = boto3.client("bedrock", region_name=settings.aws_region)
    summaries = client.list_foundation_models(byOutputModality="TEXT").get(
        "modelSummaries", []
    )
    active_text = [
        model
        for model in summaries
        if model.get("modelLifecycle", {}).get("status") == "ACTIVE"
        and "TEXT" in model.get("inputModalities", [])
        and "TEXT" in model.get("outputModalities", [])
    ]
    active_ids = {model["modelId"] for model in active_text}

    profiles_by_model: dict[str, list[str]] = {}
    for summary in _list_profiles(client):
        if summary.get("status") != "ACTIVE":
            continue
        profile_id = summary.get("inferenceProfileId")
        if not profile_id:
            continue
        try:
            detail = client.get_inference_profile(
                inferenceProfileIdentifier=profile_id
            )
        except Exception:  # noqa: BLE001 - one bad profile must not hide the catalog
            logger.exception("unable to inspect Bedrock inference profile %s", profile_id)
            continue
        for target in detail.get("models", []):
            base_id = _base_model_id_from_arn(target.get("modelArn", ""))
            if base_id in active_ids:
                profiles_by_model.setdefault(base_id, []).append(profile_id)

    updated_at = datetime.now(UTC).isoformat()
    compatible_names = {
        (model.get("providerName"), model.get("modelName"))
        for model in active_text
        if model["modelId"] in CONVERSE_TEXT_MODEL_IDS
    }
    entries: list[dict[str, Any]] = []
    for model in active_text:
        base_id = model["modelId"]
        label = model.get("modelName") or base_id
        provider_name = model.get("providerName") or "Unknown"
        inference_types = list(model.get("inferenceTypesSupported", []))
        profiles = sorted(
            set(profiles_by_model.get(base_id, [])), key=_profile_priority
        )
        provisioned_only = set(inference_types) == {"PROVISIONED"}
        converse_supported = base_id in CONVERSE_TEXT_MODEL_IDS
        if provisioned_only and (provider_name, label) in compatible_names:
            # The model family supports Converse, but this catalog alias still
            # cannot be invoked without a provisioned-model ARN.
            converse_supported = True

        invocation_id: str | None = None
        inference_type: str | None = None
        if not provisioned_only and "INFERENCE_PROFILE" in inference_types and profiles:
            invocation_id = profiles[0]
            inference_type = "inference_profile"
        elif not provisioned_only and "ON_DEMAND" in inference_types:
            invocation_id = base_id
            inference_type = "on_demand"

        disabled: list[str] = []
        if provisioned_only:
            disabled.append(
                "Provisioned-throughput alias; no provisioned-model ARN is configured"
            )
        if not converse_supported:
            disabled.append(
                NON_CONVERSE_REASONS.get(
                    base_id,
                    "Converse compatibility has not been reviewed for Marshal",
                )
            )
        if not model.get("responseStreamingSupported", False):
            disabled.append("Converse streaming is not supported")
        if invocation_id is None and not provisioned_only:
            disabled.append("No active on-demand or system inference-profile target")

        entry_id = invocation_id or base_id
        pricing = pricing_metadata(entry_id)
        if pricing.get("status") not in {"exact", "estimated_ceiling"}:
            disabled.append("No governed token price is available")

        routing_scope = None
        if inference_type == "inference_profile":
            routing_scope = (
                "us"
                if entry_id.startswith("us.")
                else "global"
                if entry_id.startswith("global.")
                else "profile"
            )
        entries.append(
            {
                "id": entry_id,
                "base_model_id": base_id,
                "label": label,
                "tier": _tier(base_id, label),
                "provider": "bedrock",
                "provider_name": provider_name,
                "source": "aws",
                "status": "ACTIVE",
                "availability": "discovered",
                "selectable": not disabled,
                "disabled_reason": "; ".join(disabled) if disabled else None,
                "converse_supported": converse_supported,
                "supports_streaming": bool(
                    model.get("responseStreamingSupported", False)
                ),
                "input_modalities": list(model.get("inputModalities", [])),
                "output_modalities": list(model.get("outputModalities", [])),
                "inference_type": inference_type or "unavailable",
                "inference_profiles": profiles,
                "routing_scope": routing_scope,
                # Listing proves regional catalog presence, not that an account
                # has completed every provider's first-use/Marketplace terms.
                "access_status": "catalog_active_unprobed",
                "pricing": pricing,
                "catalog_source": "live",
                "catalog_stale": False,
                "catalog_error": None,
                "catalog_updated_at": updated_at,
            }
        )

    entries.sort(
        key=lambda entry: (
            not entry["selectable"],
            entry["provider_name"].lower(),
            entry["label"].lower(),
            entry["base_model_id"],
        )
    )
    return entries


async def get_bedrock_catalog(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return live catalog entries, a stale in-process LKG, or static LKG.

    Only cloud tasks perform control-plane discovery. Local development and
    tests remain deterministic and network-free, while production refreshes at
    most once per six hours per task. A refresh failure never broadens access.
    """
    global _catalog_cached_at, _catalog_lkg

    if get_settings().environment.strip().lower() != "cloud":
        return copy.deepcopy(STATIC_BEDROCK_FALLBACK)

    now = time.monotonic()
    if (
        not force_refresh
        and _catalog_lkg is not None
        and now - _catalog_cached_at < CATALOG_TTL_S
    ):
        return copy.deepcopy(_catalog_lkg)

    async with _refresh_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _catalog_lkg is not None
            and now - _catalog_cached_at < CATALOG_TTL_S
        ):
            return copy.deepcopy(_catalog_lkg)
        try:
            discovered = await asyncio.to_thread(_discover_sync)
            if not any(entry["selectable"] for entry in discovered):
                raise RuntimeError("Bedrock discovery returned no selectable models")
            _catalog_lkg = discovered
            _catalog_cached_at = time.monotonic()
            logger.info(
                "Bedrock catalog refreshed: %d discovered, %d selectable",
                len(discovered),
                sum(entry["selectable"] for entry in discovered),
            )
            return copy.deepcopy(discovered)
        except Exception as exc:  # noqa: BLE001 - explicit LKG degradation
            logger.exception("Bedrock catalog refresh failed; retaining LKG")
            source = _catalog_lkg or STATIC_BEDROCK_FALLBACK
            stale = copy.deepcopy(source)
            for entry in stale:
                entry["catalog_stale"] = True
                entry["catalog_error"] = type(exc).__name__
                if entry.get("catalog_source") == "static_lkg":
                    entry["availability"] = "fallback_lkg"
            return stale


async def selectable_bedrock_catalog() -> list[dict[str, Any]]:
    """The invocation/allowlist domain, excluding visible disabled entries."""
    return [entry for entry in await get_bedrock_catalog() if entry["selectable"]]


def invalidate_catalog_cache() -> None:
    """Expire freshness without discarding the trusted in-process LKG."""
    global _catalog_cached_at
    _catalog_cached_at = 0.0
