"""Model guardrail resolution matrix (template-guardrails spec R4)."""

from unittest.mock import patch

import pytest

from app.models import Template
from app.services.guardrails import (
    GuardrailViolation,
    resolve_models,
    template_prompt_block,
)

SONNET5 = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
OPUS = "us.anthropic.claude-opus-4-8"


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def platform_defaults():
    """Isolate from the developer's .env: pin platform defaults for the matrix."""
    from app.services.platform_settings import _DEFAULTS

    class Defaults:
        bedrock_model_chat = SONNET5
        bedrock_model_spec = SONNET5
        bedrock_model_design = SONNET5
        bedrock_model_fast = HAIKU
        bedrock_max_tokens = 4096
        bedrock_max_tokens_generation = 8192

    async def default_controls():
        return _DEFAULTS

    with (
        patch("app.services.guardrails.get_settings", return_value=Defaults()),
        patch("app.services.platform_settings.get_controls", side_effect=default_controls),
    ):
        yield


def make_template(allowed=None, max_tokens=None, **kwargs) -> Template:
    model: dict = {}
    if allowed is not None:
        model["allowed_models"] = allowed
    if max_tokens is not None:
        model["max_tokens"] = max_tokens
    return Template(name="T", guardrails={"model": model} if model else {}, **kwargs)


async def test_no_template_uses_platform_defaults():
    models = await resolve_models(None)
    assert models.chat == SONNET5
    assert models.design == SONNET5
    assert models.fast == HAIKU
    assert models.template_name is None


async def test_permissive_template_keeps_defaults():
    models = await resolve_models(make_template(allowed=[SONNET5, HAIKU, OPUS]))
    assert models.chat == SONNET5
    assert models.fast == HAIKU


async def test_restrictive_template_maps_all_to_allowed():
    models = await resolve_models(make_template(allowed=[HAIKU]))
    assert models.chat == HAIKU
    assert models.requirements == HAIKU
    assert models.design == HAIKU
    assert models.tasks == HAIKU
    assert models.fast == HAIKU


async def test_tier_fallback_prefers_same_tier():
    # Sonnet 5 excluded but Sonnet 4.6 (same tier) allowed → chat maps to 4.6, not haiku
    models = await resolve_models(make_template(allowed=["us.anthropic.claude-sonnet-4-6", HAIKU]))
    assert models.chat == "us.anthropic.claude-sonnet-4-6"
    assert models.fast == HAIKU


async def test_unknown_models_only_raises_422_material():
    with pytest.raises(GuardrailViolation):
        await resolve_models(make_template(allowed=["made-up-model"]))


async def test_platform_allowlist_intersects_template():
    """S4-04 R4.3: platform allowlist is the outer bound."""
    from app.services.platform_settings import ModelControls

    haiku_only = ModelControls(
        allowlist=[HAIKU], temperature_min=0.0, temperature_max=1.0,
        top_p_min=0.0, top_p_max=1.0, max_tokens=8192, max_context=200_000,
    )

    async def haiku_controls():
        return haiku_only

    with patch("app.services.platform_settings.get_controls", side_effect=haiku_controls):
        # No template: everything falls to the only allowed model
        models = await resolve_models(None)
        assert models.chat == HAIKU and models.design == HAIKU
        # Template ∩ platform non-empty → resolves inside the intersection
        models = await resolve_models(make_template(allowed=[SONNET5, HAIKU]))
        assert models.chat == HAIKU
        # Template ∩ platform empty → 422 naming both layers
        with pytest.raises(GuardrailViolation, match="no models in common"):
            await resolve_models(make_template(allowed=[SONNET5]))


async def test_platform_max_tokens_caps_all():
    from app.services.platform_settings import ModelControls

    tight = ModelControls(
        allowlist=[SONNET5, HAIKU], temperature_min=0.0, temperature_max=1.0,
        top_p_min=0.0, top_p_max=1.0, max_tokens=2048, max_context=200_000,
    )

    async def tight_controls():
        return tight

    with patch("app.services.platform_settings.get_controls", side_effect=tight_controls):
        models = await resolve_models(None)
        assert models.max_tokens == 2048  # min(platform 2048, settings 4096)
        assert models.max_tokens_generation == 2048


async def test_max_tokens_capped_to_template():
    small = await resolve_models(make_template(allowed=[SONNET5], max_tokens=1024))
    assert small.max_tokens == 1024
    # Template larger than platform → platform cap wins
    large = await resolve_models(make_template(allowed=[SONNET5], max_tokens=99999))
    assert large.max_tokens == 4096


async def test_no_model_guardrails_means_defaults():
    models = await resolve_models(make_template())
    assert models.chat == SONNET5


async def test_prompt_block_mentions_template_and_sections():
    template = Template(
        name="Std RAG",
        description="RAG apps",
        guardrails={},
        scaffolding={"required_spec_sections": ["data_sources", "security_controls"]},
    )
    block = template_prompt_block(template)
    assert "Std RAG" in block
    assert "data_sources" in block
    assert template_prompt_block(None) == ""
