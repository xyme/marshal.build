"""B10/B11 admin surface: outbound webhooks + chat-ops (integration-wave R3.4/R4.3)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.models import User, WebhookDelivery, WebhookEndpoint
from app.services import audit
from app.services import chatops as chatops_svc
from app.services import connectors as connectors_svc
from app.services import webhooks as webhooks_svc

router = APIRouter(
    prefix="/admin/integrations",
    tags=["admin-integrations"],
    dependencies=[Depends(require_role("admin"))],
)


class WebhookIn(BaseModel):
    url: str = Field(min_length=9, max_length=512)
    event_types: list[str] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=200)


class WebhookUpdateIn(BaseModel):
    event_types: list[str] | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=200)
    active: bool | None = None


class ChatOpsIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(default=None, pattern="^(slack|teams)$")
    events: list[str] | None = None
    url: str | None = Field(default=None, max_length=512)  # write-only


@router.get("/webhooks")
async def list_webhooks(db: AsyncSession = Depends(get_db)) -> dict:
    endpoints = (
        await db.execute(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.asc()))
    ).scalars().all()
    return {
        "items": [webhooks_svc.endpoint_view(e) for e in endpoints],
        "event_types": list(webhooks_svc.WEBHOOK_EVENTS),
    }


@router.post("/webhooks", status_code=201)
async def create_webhook(
    payload: WebhookIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        created = await webhooks_svc.register(
            db, url=payload.url, event_types=payload.event_types,
            description=payload.description, actor_id=request.state.user.id,
        )
    except webhooks_svc.WebhookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # secret is in the response once; the audit trail records everything BUT it
    audit.set_audit_detail(
        request, url=created["url"], event_types=created["event_types"]
    )
    return created


@router.put("/webhooks/{endpoint_id}")
async def update_webhook(
    endpoint_id: uuid.UUID,
    payload: WebhookUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    before = webhooks_svc.endpoint_view(endpoint)
    if payload.event_types is not None:
        unknown = [e for e in payload.event_types if e not in webhooks_svc.WEBHOOK_EVENTS]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown event type(s): {', '.join(unknown)}")
        endpoint.event_types = list(dict.fromkeys(payload.event_types))
    if payload.description is not None:
        endpoint.description = payload.description.strip()[:200] or None
    if payload.active is not None:
        endpoint.active = payload.active
    await db.commit()
    after = webhooks_svc.endpoint_view(endpoint)
    audit.set_audit_detail(request, before=before, after=after)
    return after


@router.delete("/webhooks/{endpoint_id}")
async def delete_webhook(
    endpoint_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    audit.set_audit_detail(request, url=endpoint.url)
    await db.delete(endpoint)  # deliveries cascade
    await db.commit()
    return {"deleted": True}


@router.post("/webhooks/{endpoint_id}/test")
async def test_webhook(
    endpoint_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    result = await webhooks_svc.send_test(db, endpoint)
    audit.set_audit_detail(request, url=endpoint.url, ok=result.get("ok"))
    return result


@router.get("/webhooks/{endpoint_id}/deliveries")
async def webhook_deliveries(
    endpoint_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": str(d.id),
                "event_type": d.event_type,
                "status": d.status,
                "attempts": d.attempts,
                "last_status_code": d.last_status_code,
                "last_error": d.last_error,
                "created_at": d.created_at.isoformat(),
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
            }
            for d in rows
        ]
    }


# ------------------------------------------------------------------ chat-ops


@router.get("/chat-ops")
async def get_chat_ops(db: AsyncSession = Depends(get_db)) -> dict:
    config = await chatops_svc.get_config()
    url = await chatops_svc._url()  # noqa: SLF001 — presence check only
    return {**config, "has_url": bool(url), "event_types": list(webhooks_svc.WEBHOOK_EVENTS)}


@router.put("/chat-ops")
async def put_chat_ops(
    payload: ChatOpsIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    body = payload.model_dump(exclude_none=True)
    url = body.pop("url", None)
    try:
        config = await chatops_svc.update_config(db, body, url=url)
    except chatops_svc.ChatOpsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, **{**config, "url_updated": url is not None})
    return {**config, "has_url": bool(await chatops_svc._url())}  # noqa: SLF001


@router.post("/chat-ops/test")
async def test_chat_ops(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await chatops_svc.send_test(db)
    audit.set_audit_detail(request, ok=result.get("ok"))
    return result


# ------------------------------------------------------------- C0 connectors
# Registry + reachability only (external-import-connectors spec): generated
# agents do NOT consume these yet — that is the decision-gated C1 phase.


class ConnectorIn(BaseModel):
    slug: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern="^(data_source|http_api|agent_registry|mcp_server)$")
    base_url: str = Field(min_length=9, max_length=512)
    description: str | None = Field(default=None, max_length=500)
    auth_header: str | None = Field(default=None, max_length=64)
    credential: str | None = Field(default=None, max_length=4096)  # write-only


class ConnectorUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    type: str | None = Field(
        default=None, pattern="^(data_source|http_api|agent_registry|mcp_server)$"
    )
    base_url: str | None = Field(default=None, min_length=9, max_length=512)
    description: str | None = Field(default=None, max_length=500)
    auth_header: str | None = Field(default=None, max_length=64)
    credential: str | None = Field(default=None, max_length=4096)  # write-only
    clear_credential: bool = False


class ConnectorStatusIn(BaseModel):
    active: bool


@router.get("/connectors")
async def list_connectors(db: AsyncSession = Depends(get_db)) -> dict:
    return {
        "items": await connectors_svc.list_connectors(db),
        "types": list(connectors_svc.CONNECTOR_TYPES),
    }


@router.post("/connectors", status_code=201)
async def create_connector(
    payload: ConnectorIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict:
    try:
        view = await connectors_svc.create_connector(
            db, payload.model_dump(exclude_none=True), admin.id
        )
    except connectors_svc.ConnectorValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The credential is write-only: audit records everything BUT it.
    audit.set_audit_detail(
        request, slug=view["slug"], type=view["type"], base_url=view["base_url"],
        has_credential=view["has_credential"],
    )
    return view


@router.put("/connectors/{slug}")
async def update_connector(
    slug: str,
    payload: ConnectorUpdateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict:
    try:
        view, change = await connectors_svc.update_connector(
            db, slug, payload.model_dump(exclude_none=True) | (
                {"clear_credential": True} if payload.clear_credential else {}
            ), admin.id,
        )
    except connectors_svc.ConnectorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except connectors_svc.ConnectorValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, **change)
    return view


@router.post("/connectors/{slug}/probe")
async def probe_connector(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        result = await connectors_svc.probe(db, slug)
    except connectors_svc.ConnectorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except connectors_svc.ProbeBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.set_audit_detail(request, slug=slug, ok=result.get("ok"), status=result.get("status"))
    return result


@router.put("/connectors/{slug}/status")
async def set_connector_status(
    slug: str,
    payload: ConnectorStatusIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict:
    try:
        view = await connectors_svc.set_connector_status(db, slug, payload.active, admin.id)
    except connectors_svc.ConnectorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit.set_audit_detail(request, slug=slug, active=payload.active)
    return view


@router.delete("/connectors/{slug}")
async def delete_connector(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_user),
) -> dict:
    try:
        view = await connectors_svc.delete_connector(db, slug, admin.id)
    except connectors_svc.ConnectorNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit.set_audit_detail(request, slug=slug, type=view.get("type"))
    return {"deleted": slug}
