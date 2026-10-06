"""Notifications API (notifications spec R4) — strictly owner-scoped."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models import Notification, User
from app.services import notifications as svc

router = APIRouter(prefix="/notifications", tags=["notifications"])

TYPE_GROUPS: dict[str, list[str]] = {
    "generation": ["generation_complete"],
    "deployments": [
        "deploy_succeeded", "deploy_failed",
        # S11 lifecycle events (drill-found: these were invisible to the filter)
        "deployment_degraded", "deployment_expiring", "deployment_expired",
    ],
    "cost": ["cost_threshold", "cost_cap_reached"],
    "risk": [
        "risk_review_requested", "risk_decided",
        "risk_changes_requested", "risk_escalated",
    ],
    "marketplace": ["sample_published"],
}


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None
    read_at: datetime | None
    created_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    page: int
    page_size: int


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    unread: bool | None = None,
    group: str | None = Query(None, pattern="^(generation|deployments|cost|risk|marketplace)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationListOut:
    from app.core.tenant import tenant_scope

    stmt = tenant_scope(
        select(Notification).where(Notification.user_id == user.id), Notification
    )
    if unread is True:
        stmt = stmt.where(Notification.read_at.is_(None))
    elif unread is False:
        stmt = stmt.where(Notification.read_at.is_not(None))
    if group:
        stmt = stmt.where(Notification.type.in_(TYPE_GROUPS[group]))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(
        stmt.order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return NotificationListOut(
        items=[NotificationOut.model_validate(n) for n in result.scalars()],
        total=total, page=page, page_size=page_size,
    )


@router.get("/unread-count")
async def get_unread_count(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"unread": await svc.unread_count(db, user.id)}


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationOut:
    row = await db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
    return NotificationOut.model_validate(row)


@router.post("/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await db.commit()
    return {"marked": result.rowcount}
