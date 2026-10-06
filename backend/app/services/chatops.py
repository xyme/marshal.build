"""B11 chat-ops channel (integration-wave spec R4): ONE org-level Slack/Teams
incoming webhook riding the notify() seam. The URL is a secret (Secrets
Manager, marshal/chatops/url — write-only, responses carry has_url).
Failures log and drop: in-app is the guaranteed channel, this one is comfort.
"""

import asyncio
import logging
import time

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import PlatformSettings
from app.services.webhooks import WEBHOOK_EVENTS

logger = logging.getLogger("marshal.chatops")

SECRET_NAME = "marshal/chatops/url"
PROVIDERS = ("slack", "teams")
POST_TIMEOUT_S = 8
CONFIG_CACHE_TTL_S = 60

# notify_many fan-out dedupe: one post per event, not per recipient. In-task
# memory is enough — a group fan-out runs inside one task.
_recent_keys: dict[str, float] = {}
_RECENT_TTL_S = 300


class ChatOpsValidationError(Exception):
    """Invalid config — surfaces as 422."""


_secrets_client = None
_url_cache: tuple[float, str | None] | None = None
_config_cache: tuple[float, dict] | None = None


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client(
            "secretsmanager", region_name=get_settings().aws_region
        )
    return _secrets_client


def _store_url(url: str) -> None:
    global _url_cache
    try:
        try:
            _secrets().create_secret(Name=SECRET_NAME, SecretString=url)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
            _secrets().put_secret_value(SecretId=SECRET_NAME, SecretString=url)
    except (ClientError, BotoCoreError) as exc:
        raise ChatOpsValidationError(f"Could not store the webhook URL: {exc}") from exc
    _url_cache = None


async def _url() -> str | None:
    global _url_cache
    now = time.monotonic()
    if _url_cache and now - _url_cache[0] < 300:
        return _url_cache[1]

    def read() -> str | None:
        try:
            return _secrets().get_secret_value(SecretId=SECRET_NAME).get("SecretString")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise

    url = await asyncio.to_thread(read)
    _url_cache = (now, url)
    return url


def invalidate_cache() -> None:
    global _config_cache, _url_cache
    _config_cache = None
    _url_cache = None


async def get_config() -> dict:
    """{enabled, provider, events, has_url is resolved separately} — cached."""
    global _config_cache
    now = time.monotonic()
    if _config_cache and now - _config_cache[0] < CONFIG_CACHE_TTL_S:
        return _config_cache[1]
    config = {"enabled": False, "provider": "slack", "events": []}
    try:
        from app.core.db import SessionLocal

        async with SessionLocal() as db:
            row = await db.get(PlatformSettings, 1)
        stored = (getattr(row, "chat_ops", None) or {}) if row else {}
        config.update({k: v for k, v in stored.items() if k in config})
    except Exception:  # noqa: BLE001 — a settings read must not break notify()
        logger.exception("chat-ops config read failed; channel off")
    _config_cache = (now, config)
    return config


def validate_config(payload: dict) -> dict:
    out: dict = {}
    unknown = [k for k in payload if k not in ("enabled", "provider", "events")]
    if unknown:
        raise ChatOpsValidationError(f"Unknown keys: {', '.join(sorted(unknown))}")
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ChatOpsValidationError("enabled must be true or false")
        out["enabled"] = payload["enabled"]
    if "provider" in payload:
        if payload["provider"] not in PROVIDERS:
            raise ChatOpsValidationError(f"provider must be one of {', '.join(PROVIDERS)}")
        out["provider"] = payload["provider"]
    if "events" in payload:
        events = payload["events"]
        if not isinstance(events, list):
            raise ChatOpsValidationError("events must be a list")
        bad = [e for e in events if e not in WEBHOOK_EVENTS]
        if bad:
            raise ChatOpsValidationError(
                f"Unknown event type(s): {', '.join(bad)}"
            )
        out["events"] = list(dict.fromkeys(events))
    return out


async def update_config(db: AsyncSession, payload: dict, *, url: str | None) -> dict:
    fields = validate_config(payload)
    if url is not None:
        url = url.strip()
        if not url.lower().startswith("https://"):
            raise ChatOpsValidationError("Webhook URL must use https://")
        _store_url(url)
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
    row.chat_ops = {**(getattr(row, "chat_ops", None) or {}), **fields}
    await db.commit()
    invalidate_cache()
    return dict(row.chat_ops)


def _message(title: str, body: str, link: str | None) -> dict:
    settings = get_settings()
    base = getattr(settings, "app_base_url", "http://localhost:3000")
    absolute = f"{base}{link}" if link and link.startswith("/") else link
    text = f"*{title}*"
    if body:
        text += f"\n{body}"
    if absolute:
        text += f"\n{absolute}"
    # Slack and Teams incoming webhooks both accept {"text": ...}
    return {"text": text}


async def post(
    event_type: str, *, title: str, body: str = "", link: str | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Fire-and-forget channel post; silent degradation (R4.2)."""
    try:
        config = await get_config()
        if not config.get("enabled") or event_type not in (config.get("events") or []):
            return
        if dedupe_key:
            now = time.monotonic()
            for key, ts in list(_recent_keys.items()):
                if now - ts > _RECENT_TTL_S:
                    _recent_keys.pop(key, None)
            marker = f"{event_type}:{dedupe_key}"
            if marker in _recent_keys:
                return
            _recent_keys[marker] = now
        url = await _url()
        if not url:
            return
        async with httpx.AsyncClient(timeout=POST_TIMEOUT_S) as client:
            response = await client.post(url, json=_message(title, body, link))
        if response.status_code >= 300:
            logger.warning("chat-ops post failed: HTTP %s", response.status_code)
    except Exception:  # noqa: BLE001 — comfort channel, never disturbs producers
        logger.exception("chat-ops post failed")


async def send_test(db: AsyncSession) -> dict:
    """Admin test — verbatim result, bypasses enabled/event routing."""
    url = await _url()
    if not url:
        return {"ok": False, "error": "No webhook URL configured"}
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=POST_TIMEOUT_S) as client:
            response = await client.post(
                url,
                json=_message(
                    "marshal chat-ops test",
                    "Channel wiring verified — platform events will post here.",
                    "/admin/costs",
                ),
            )
        return {
            "ok": response.status_code < 300,
            "status_code": response.status_code,
            "rtt_ms": int((time.monotonic() - started) * 1000),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)[:300]}
