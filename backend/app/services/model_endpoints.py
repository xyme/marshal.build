"""Custom OpenAI-compatible model endpoints — admin lifecycle (S17-02/03).

The endpoint list lives on the platform-settings singleton (JSONB); API keys
live in Secrets Manager (`marshal/models/<slug>`) and NEVER touch Postgres or
API responses. Slugs are immutable after create: they anchor the secret name,
the registry id (`ext/<slug>`), and every historical invocation row.

Pricing is REQUIRED at registration [D15]: an endpoint without a price would
silently exempt itself from every cost cap the platform promises admins.
"""

import asyncio
import logging
import re
import time
import uuid

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import PlatformSettings
from app.services.platform_settings import invalidate_cache

logger = logging.getLogger("marshal.model_endpoints")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
TIERS = ("standard", "fast", "advanced")
SECRET_PREFIX = "marshal/models/"
KEY_CACHE_TTL_S = 300

# Fields an admin may set; everything else on the stored dict is derived.
_MUTABLE_FIELDS = (
    "label", "base_url", "model_name", "tier", "usd_per_1k_input",
    "usd_per_1k_output", "max_context_tokens", "timeout_s", "enabled",
)


class EndpointValidationError(Exception):
    """Invalid endpoint payload — surfaces as 422."""


class EndpointNotFound(Exception):
    """Unknown slug — surfaces as 404."""


def _validate_fields(data: dict, *, creating: bool) -> dict:
    """Normalise + validate a payload; returns only known, cleaned fields."""
    out: dict = {}
    if creating:
        slug = (data.get("slug") or "").strip()
        if not SLUG_RE.match(slug):
            raise EndpointValidationError(
                "slug must be 3-40 chars of lowercase letters, digits and hyphens "
                "(it becomes the model id `ext/<slug>` and is permanent)"
            )
        out["slug"] = slug
    label = data.get("label")
    if label is not None:
        label = label.strip()
        if not label or len(label) > 80:
            raise EndpointValidationError("label is required (max 80 characters)")
        out["label"] = label
    base_url = data.get("base_url")
    if base_url is not None:
        base_url = base_url.strip().rstrip("/")
        if not base_url.lower().startswith("https://"):
            raise EndpointValidationError(
                "base_url must use https:// — plaintext endpoints are not "
                "accepted (D18: public HTTPS is the approved network path)"
            )
        if len(base_url) > 512:
            raise EndpointValidationError("base_url too long (max 512 characters)")
        out["base_url"] = base_url
    model_name = data.get("model_name")
    if model_name is not None:
        model_name = model_name.strip()
        if not model_name or len(model_name) > 120:
            raise EndpointValidationError("model_name is required (max 120 characters)")
        out["model_name"] = model_name
    tier = data.get("tier")
    if tier is not None:
        if tier not in TIERS:
            raise EndpointValidationError(f"tier must be one of {', '.join(TIERS)}")
        out["tier"] = tier
    for price_field in ("usd_per_1k_input", "usd_per_1k_output"):
        value = data.get(price_field)
        if value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise EndpointValidationError(f"{price_field} must be a number") from exc
            if not (0 < value <= 10):
                raise EndpointValidationError(
                    f"{price_field} must be > 0 and ≤ 10 USD — required so cost "
                    "caps govern this endpoint [D15]"
                )
            out[price_field] = value
    max_context = data.get("max_context_tokens")
    if max_context is not None:
        max_context = int(max_context)
        if not (1024 <= max_context <= 2_000_000):
            raise EndpointValidationError("max_context_tokens must be 1024–2000000")
        out["max_context_tokens"] = max_context
    timeout_s = data.get("timeout_s")
    if timeout_s is not None:
        timeout_s = int(timeout_s)
        if not (5 <= timeout_s <= 300):
            raise EndpointValidationError("timeout_s must be 5–300 seconds")
        out["timeout_s"] = timeout_s
    enabled = data.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise EndpointValidationError("enabled must be true or false")
        out["enabled"] = enabled
    return out


def _require_complete(entry: dict) -> None:
    """Creation must supply everything the runtime later depends on."""
    missing = [
        f for f in ("label", "base_url", "model_name", "usd_per_1k_input", "usd_per_1k_output")
        if f not in entry
    ]
    if missing:
        raise EndpointValidationError(f"Missing required fields: {', '.join(missing)}")


def public_view(entry: dict) -> dict:
    """What leaves the API: everything except any hint of the key value."""
    return {
        "slug": entry["slug"],
        "id": f"ext/{entry['slug']}",
        "label": entry.get("label"),
        "base_url": entry.get("base_url"),
        "model_name": entry.get("model_name"),
        "tier": entry.get("tier", "standard"),
        "usd_per_1k_input": entry.get("usd_per_1k_input"),
        "usd_per_1k_output": entry.get("usd_per_1k_output"),
        "max_context_tokens": entry.get("max_context_tokens", 8192),
        "timeout_s": entry.get("timeout_s", 60),
        "has_api_key": bool(entry.get("has_api_key")),
        "enabled": entry.get("enabled", True),
    }


async def _row(db: AsyncSession) -> PlatformSettings:
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
        await db.flush()
    return row


def _find(entries: list, slug: str) -> dict:
    for entry in entries:
        if entry.get("slug") == slug:
            return entry
    raise EndpointNotFound(f"No custom endpoint '{slug}'")


# ------------------------------------------------------------- secret store

_secrets_client = None
_key_cache: dict[str, tuple[float, str | None]] = {}


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client(
            "secretsmanager", region_name=get_settings().aws_region
        )
    return _secrets_client


def _store_key(slug: str, api_key: str) -> None:
    name = f"{SECRET_PREFIX}{slug}"
    try:
        try:
            _secrets().create_secret(Name=name, SecretString=api_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
            _secrets().put_secret_value(SecretId=name, SecretString=api_key)
    except (ClientError, BotoCoreError) as exc:
        raise EndpointValidationError(f"Could not store the API key: {exc}") from exc
    _key_cache.pop(slug, None)


async def api_key_for(slug: str) -> str | None:
    """Call-time key read with a short cache; None = endpoint has no key."""
    cached = _key_cache.get(slug)
    now = time.monotonic()
    if cached and now - cached[0] < KEY_CACHE_TTL_S:
        return cached[1]

    def read() -> str | None:
        try:
            resp = _secrets().get_secret_value(SecretId=f"{SECRET_PREFIX}{slug}")
            return resp.get("SecretString")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise

    key = await asyncio.to_thread(read)
    _key_cache[slug] = (now, key)
    return key


# ---------------------------------------------------------------- lifecycle


async def list_endpoints(db: AsyncSession) -> list[dict]:
    row = await db.get(PlatformSettings, 1)
    entries = list(getattr(row, "custom_model_endpoints", None) or []) if row else []
    return [public_view(e) for e in entries]


async def create_endpoint(db: AsyncSession, payload: dict, actor_id: uuid.UUID) -> dict:
    fields = _validate_fields(payload, creating=True)
    fields.setdefault("tier", "standard")
    fields.setdefault("max_context_tokens", 8192)
    fields.setdefault("timeout_s", 60)
    fields.setdefault("enabled", True)
    _require_complete(fields)
    row = await _row(db)
    entries = list(row.custom_model_endpoints or [])
    if any(e.get("slug") == fields["slug"] for e in entries):
        raise EndpointValidationError(f"Endpoint '{fields['slug']}' already exists")
    api_key = (payload.get("api_key") or "").strip()
    fields["has_api_key"] = bool(api_key)
    if api_key:
        _store_key(fields["slug"], api_key)
    entries.append(fields)
    row.custom_model_endpoints = entries
    row.updated_by = actor_id
    await db.commit()
    invalidate_cache()
    logger.info("custom endpoint created: %s (%s)", fields["slug"], fields["base_url"])
    return public_view(fields)


async def update_endpoint(
    db: AsyncSession, slug: str, payload: dict, actor_id: uuid.UUID
) -> tuple[dict, dict]:
    """Returns (public_view, {before, after}) for audit enrichment."""
    if "slug" in payload and payload["slug"] != slug:
        raise EndpointValidationError("slug is immutable — create a new endpoint instead")
    fields = _validate_fields(payload, creating=False)
    row = await _row(db)
    entries = list(row.custom_model_endpoints or [])
    entry = _find(entries, slug)
    before = public_view(entry)
    entry.update({k: v for k, v in fields.items() if k in _MUTABLE_FIELDS})
    api_key = (payload.get("api_key") or "").strip()
    if api_key:
        _store_key(slug, api_key)
        entry["has_api_key"] = True
    row.custom_model_endpoints = entries
    row.updated_by = actor_id
    # JSONB list mutation in place — force the ORM to see the change
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(row, "custom_model_endpoints")
    await db.commit()
    invalidate_cache()
    after = public_view(entry)
    logger.info("custom endpoint updated: %s (enabled=%s)", slug, entry.get("enabled"))
    return after, {"before": before, "after": after}


async def endpoint_config(slug: str) -> dict:
    """Runtime lookup through the cached controls (adapter + probe path)."""
    from app.services.platform_settings import get_controls

    for entry in (await get_controls()).custom_endpoints:
        if entry.get("slug") == slug:
            return entry
    raise EndpointNotFound(f"No custom endpoint '{slug}'")


# -------------------------------------------------------------------- probe

_probe_inflight: set[str] = set()


class ProbeBusy(Exception):
    """A probe for this endpoint is already running — surfaces as 409."""


async def probe(slug: str, entry: dict | None = None) -> dict:
    """1-token completion + streaming check. Answers 'is it up NOW' — no cache.

    Errors return verbatim class + message (S15-05: a 401 and a timeout are
    different fixes; hiding which one wastes the admin's evening).
    """
    if slug in _probe_inflight:
        raise ProbeBusy(f"A probe for '{slug}' is already running")
    _probe_inflight.add(slug)
    try:
        cfg = entry or await endpoint_config(slug)
        headers = {"Content-Type": "application/json"}
        key = await api_key_for(slug)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = f"{cfg['base_url']}/chat/completions"
        payload = {
            "model": cfg["model_name"],
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        timeout = min(int(cfg.get("timeout_s", 60)), 15)
        result: dict = {"ok": False, "streaming": False}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                result["rtt_ms"] = int((time.monotonic() - started) * 1000)
                if resp.status_code != 200:
                    result["error_class"] = f"HTTP_{resp.status_code}"
                    result["error"] = resp.text[:300]
                    return result
                body = resp.json()
                result["ok"] = True
                result["model_echo"] = body.get("model")
                # streaming support: open a stream and read the first line
                try:
                    async with client.stream(
                        "POST", url, json={**payload, "stream": True}, headers=headers
                    ) as stream_resp:
                        if stream_resp.status_code == 200:
                            async for _line in stream_resp.aiter_lines():
                                result["streaming"] = True
                                break
                except httpx.HTTPError:
                    result["streaming"] = False
        except httpx.HTTPError as exc:
            result["rtt_ms"] = int((time.monotonic() - started) * 1000)
            result["error_class"] = type(exc).__name__
            result["error"] = str(exc)[:300]
        return result
    finally:
        _probe_inflight.discard(slug)
