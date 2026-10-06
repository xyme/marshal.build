"""Versioned Bedrock token pricing used by spend enforcement (R2.3).

AWS's Price List does not currently publish a complete, consistently named set
of SKUs for every Bedrock Marketplace/system-profile model. Known rates stay
exact; every other governed Bedrock model is charged internally at an explicit
high ceiling. The ceiling intentionally overestimates spend so a newly exposed
model can block a budget early but can never bypass caps with a NULL cost.
"""

from decimal import Decimal

PRICING_VERSION = 2

# model_id → (USD per 1K input tokens, USD per 1K output tokens).
PRICING_MAP: dict[str, tuple[Decimal, Decimal]] = {
    "us.anthropic.claude-sonnet-5": (Decimal("0.003"), Decimal("0.015")),
    "us.anthropic.claude-sonnet-4-6": (Decimal("0.003"), Decimal("0.015")),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (
        Decimal("0.001"),
        Decimal("0.005"),
    ),
    "us.anthropic.claude-opus-4-8": (Decimal("0.015"), Decimal("0.075")),
    # Historical id retained so old invocation rows remain reproducible.
    "us.anthropic.claude-opus-4-1-20250805-v1:0": (
        Decimal("0.015"),
        Decimal("0.075"),
    ),
}

# Deliberately above the highest reviewed standard on-demand text rate. This is
# a governance estimate, not a claim about provider billing.
ESTIMATED_CEILING_PRICE = (Decimal("0.100"), Decimal("0.500"))


def _governed_bedrock_model(model_id: str) -> bool:
    """Whether an invocation target maps to the reviewed catalog overlay."""
    from app.services.bedrock_catalog import CONVERSE_TEXT_MODEL_IDS

    base_id = model_id
    if base_id.startswith("us.") or base_id.startswith("global."):
        base_id = base_id.split(".", 1)[1]
    return base_id in CONVERSE_TEXT_MODEL_IDS


def price_for_model(
    model_id: str,
) -> tuple[Decimal, Decimal, str] | None:
    exact = PRICING_MAP.get(model_id)
    if exact is not None:
        return (*exact, "exact")
    if _governed_bedrock_model(model_id):
        return (*ESTIMATED_CEILING_PRICE, "estimated_ceiling")
    return None


def pricing_metadata(model_id: str) -> dict:
    price = price_for_model(model_id)
    if price is None:
        return {
            "version": PRICING_VERSION,
            "status": "unavailable",
            "input_usd_per_1k": None,
            "output_usd_per_1k": None,
        }
    per_in, per_out, status = price
    return {
        "version": PRICING_VERSION,
        "status": status,
        "input_usd_per_1k": float(per_in),
        "output_usd_per_1k": float(per_out),
    }


def compute_cost_usd(
    model_id: str, input_tokens: int, output_tokens: int
) -> Decimal | None:
    price = price_for_model(model_id)
    if price is None:
        return None
    per_in, per_out, _status = price
    return (per_in * input_tokens + per_out * output_tokens) / 1000


async def compute_cost_ext(
    model_id: str, input_tokens: int, output_tokens: int
) -> Decimal | None:
    """Price an external (`ext/<slug>`) call from admin-entered rates (S17-05).

    Rates are REQUIRED at endpoint registration [D15], so None is reachable
    only if the endpoint row vanished from settings mid-flight — logged
    unpriced by the caller, exactly like an unknown Bedrock id.

    Reads through the cached controls and deliberately ignores `enabled`:
    an in-flight call on a just-disabled endpoint must still be priced.
    """
    from app.services.platform_settings import get_controls

    slug = model_id.removeprefix("ext/")
    for endpoint in (await get_controls()).custom_endpoints:
        if endpoint.get("slug") == slug:
            per_in = endpoint.get("usd_per_1k_input")
            per_out = endpoint.get("usd_per_1k_output")
            if per_in is None or per_out is None:
                return None
            return (
                Decimal(str(per_in)) * input_tokens
                + Decimal(str(per_out)) * output_tokens
            ) / 1000
    return None
