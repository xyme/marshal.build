"""B10 outbound webhooks (integration-wave spec R3).

One more channel on the notify() seam — endpoint-scoped, so it hooks BEFORE
the per-user preference gates (subscriptions are not user prefs) and dedupes
per endpoint (the notify_many fan-out posts once per event, not once per
recipient). Delivery is at-least-once: an immediate attempt on dispatch, then
bounded retries from the elected-scheduler sweep over durable rows (the
platform's answer to in-process work dying with the task). Failures NEVER
block the producing operation.

Signing: per-endpoint secret in Secrets Manager (marshal/webhooks/<id>, the
S17 custody pattern — shown once at registration, never in Postgres or
responses). `X-Marshal-Signature: t=<unix>,v1=<hmac_sha256(secret, f"{t}.{body}")>`
with the timestamp bounding the replay window consumer-side.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Alert, WebhookDelivery, WebhookEndpoint

logger = logging.getLogger("marshal.webhooks")

SECRET_PREFIX = "marshal/webhooks/"
SECRET_CACHE_TTL_S = 300
MAX_ATTEMPTS = 5
DELIVERY_TIMEOUT_S = 10
SWEEP_INTERVAL_S = 60

# v1 event allowlist (owner decision): build/deployment/risk lifecycle only.
WEBHOOK_EVENTS = (
    "build_ready",
    "build_failed",
    "deploy_succeeded",
    "deploy_failed",
    "deployment_degraded",
    "deployment_expiring",
    "deployment_expired",
    "risk_decided",
    "risk_changes_requested",
)


class WebhookValidationError(Exception):
    """Invalid registration payload — surfaces as 422."""


_secrets_client = None
_secret_cache: dict[str, tuple[float, str | None]] = {}


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client(
            "secretsmanager", region_name=get_settings().aws_region
        )
    return _secrets_client


def _store_secret(endpoint_id: str, secret: str) -> None:
    name = f"{SECRET_PREFIX}{endpoint_id}"
    try:
        try:
            _secrets().create_secret(Name=name, SecretString=secret)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
            _secrets().put_secret_value(SecretId=name, SecretString=secret)
    except (ClientError, BotoCoreError) as exc:
        raise WebhookValidationError(f"Could not store the signing secret: {exc}") from exc
    _secret_cache.pop(endpoint_id, None)


async def _secret_for(endpoint_id: str) -> str | None:
    cached = _secret_cache.get(endpoint_id)
    now = time.monotonic()
    if cached and now - cached[0] < SECRET_CACHE_TTL_S:
        return cached[1]

    def read() -> str | None:
        try:
            resp = _secrets().get_secret_value(SecretId=f"{SECRET_PREFIX}{endpoint_id}")
            return resp.get("SecretString")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise

    secret = await asyncio.to_thread(read)
    _secret_cache[endpoint_id] = (now, secret)
    return secret


def sign(secret: str, timestamp: int, body: str) -> str:
    digest = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


# ---------------------------------------------------------------- lifecycle


def _validate(url: str, event_types: list[str]) -> None:
    if not (url or "").strip().lower().startswith("https://"):
        raise WebhookValidationError("Webhook URLs must use https://")
    if len(url) > 512:
        raise WebhookValidationError("URL too long (max 512 characters)")
    unknown = [e for e in event_types if e not in WEBHOOK_EVENTS]
    if unknown:
        raise WebhookValidationError(
            f"Unknown event type(s): {', '.join(unknown)} — "
            f"v1 covers: {', '.join(WEBHOOK_EVENTS)}"
        )
    if not event_types:
        raise WebhookValidationError("Subscribe to at least one event type")


async def register(
    db: AsyncSession, *, url: str, event_types: list[str],
    description: str | None, actor_id: uuid.UUID,
) -> dict:
    """Returns the endpoint view PLUS the signing secret — the only time the
    secret ever appears in a response (R3.1)."""
    _validate(url, event_types)
    endpoint = WebhookEndpoint(
        url=url.strip(), event_types=list(dict.fromkeys(event_types)),
        description=(description or "").strip()[:200] or None, created_by=actor_id,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    secret = f"whsec_{pysecrets.token_urlsafe(32)}"
    _store_secret(str(endpoint.id), secret)
    return {**endpoint_view(endpoint), "secret": secret}


def endpoint_view(endpoint: WebhookEndpoint) -> dict:
    return {
        "id": str(endpoint.id),
        "url": endpoint.url,
        "description": endpoint.description,
        "event_types": list(endpoint.event_types or []),
        "active": bool(endpoint.active),
        "created_at": endpoint.created_at.isoformat(),
    }


# ---------------------------------------------------------------- dispatch


def _payload(event_type: str, title: str, body: str, link: str | None) -> dict:
    settings = get_settings()
    base = getattr(settings, "app_base_url", "http://localhost:3000")
    return {
        "event": event_type,
        "title": title,
        "body": body,
        "link": f"{base}{link}" if link and link.startswith("/") else link,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


async def dispatch(
    event_type: str, *, title: str, body: str = "", link: str | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Create delivery rows for subscribed active endpoints + attempt now.
    Fresh session (called fire-and-forget off the notify() seam); never raises."""
    if event_type not in WEBHOOK_EVENTS:
        return
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        endpoints = (
            await db.execute(
                select(WebhookEndpoint).where(WebhookEndpoint.active.is_(True))
            )
        ).scalars().all()
        targets = [e for e in endpoints if event_type in (e.event_types or [])]
        if not targets:
            return
        payload = _payload(event_type, title, body, link)
        deliveries: list[uuid.UUID] = []
        for endpoint in targets:
            row = WebhookDelivery(
                endpoint_id=endpoint.id,
                event_type=event_type,
                payload=payload,
                status="pending",
                next_attempt_at=datetime.now(UTC),
                dedupe_key=f"{event_type}:{dedupe_key}" if dedupe_key else None,
            )
            db.add(row)
            try:
                await db.commit()
                deliveries.append(row.id)
            except IntegrityError:  # dedupe hit — the fan-out already delivered
                await db.rollback()
        for delivery_id in deliveries:
            await _attempt(db, delivery_id)


async def _attempt(db: AsyncSession, delivery_id: uuid.UUID) -> None:
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.status in ("delivered", "dead"):
        return
    endpoint = await db.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is None or not endpoint.active:
        delivery.status = "dead"
        delivery.last_error = "endpoint removed or deactivated"
        await db.commit()
        return
    body = json.dumps({**delivery.payload, "delivery_id": str(delivery.id)})
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "user-agent": "marshal-webhooks/1.0",
        "x-marshal-event": delivery.event_type,
        "x-marshal-delivery": str(delivery.id),
    }
    secret = await _secret_for(str(endpoint.id))
    if secret:
        headers["x-marshal-signature"] = sign(secret, timestamp, body)
    delivery.attempts += 1
    error: str | None = None
    status_code: int | None = None
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
            response = await client.post(endpoint.url, content=body, headers=headers)
        status_code = response.status_code
        if 200 <= response.status_code < 300:
            delivery.status = "delivered"
            delivery.delivered_at = datetime.now(UTC)
            delivery.last_status_code = status_code
            delivery.last_error = None
            await db.commit()
            return
        error = f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        error = str(exc)[:300]
    delivery.last_status_code = status_code
    delivery.last_error = error
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "dead"
        db.add(
            Alert(
                kind="webhook",
                severity="warning",
                message=(
                    f"Webhook delivery to {endpoint.url} is DEAD after "
                    f"{MAX_ATTEMPTS} attempts ({error}) — check the endpoint "
                    "under Admin → Integrations."
                ),
                dedupe_key=f"webhook_dead:{endpoint.id}:{datetime.now(UTC):%Y-%m-%d}",
            )
        )
    else:
        delivery.status = "failed"
        delivery.next_attempt_at = datetime.now(UTC) + timedelta(
            minutes=2 ** delivery.attempts
        )
    try:
        await db.commit()
    except IntegrityError:  # alert dedupe collision — keep the delivery state
        await db.rollback()
        delivery2 = await db.get(WebhookDelivery, delivery_id)
        if delivery2 is not None:
            delivery2.attempts = delivery.attempts
            delivery2.status = delivery.status
            delivery2.last_error = error
            delivery2.last_status_code = status_code
            await db.commit()


async def delivery_tick() -> None:
    """Retry sweep (elected loop): due failed/pending rows, oldest first."""
    from app.core.db import SessionLocal

    now = datetime.now(UTC)
    async with SessionLocal() as db:
        due = (
            await db.execute(
                select(WebhookDelivery.id)
                .where(
                    WebhookDelivery.status.in_(("pending", "failed")),
                    WebhookDelivery.next_attempt_at.is_not(None),
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at.asc())
                .limit(25)
            )
        ).scalars().all()
        for delivery_id in due:
            await _attempt(db, delivery_id)


async def send_test(db: AsyncSession, endpoint: WebhookEndpoint) -> dict:
    """Admin test post — verbatim result (the S15-05 probe discipline)."""
    payload = _payload(
        "test", "marshal webhook test",
        "If your receiver verified the signature, the wiring is complete.", None,
    )
    body = json.dumps({**payload, "delivery_id": "test"})
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "x-marshal-event": "test",
        "x-marshal-delivery": "test",
    }
    secret = await _secret_for(str(endpoint.id))
    if secret:
        headers["x-marshal-signature"] = sign(secret, timestamp, body)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S) as client:
            response = await client.post(endpoint.url, content=body, headers=headers)
        return {
            "ok": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "rtt_ms": int((time.monotonic() - started) * 1000),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)[:300]}
