"""Usage analytics admin endpoints (S16-04) — read-only.

Events are written server-side at the instrumented seams (services/analytics);
there is deliberately NO ingest endpoint here: nothing client-supplied enters
the events table, which keeps the analytics story honest and injection-free.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_role
from app.core.db import get_db
from app.services import analytics as analytics_svc

admin_router = APIRouter(
    prefix="/admin/analytics",
    tags=["admin-analytics"],
    dependencies=[Depends(require_role("admin"))],
)


@admin_router.get("/summary")
async def analytics_summary(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await analytics_svc.summary(db, days=days)


@admin_router.get("/funnel")
async def analytics_funnel(
    days: int = Query(default=30, ge=1, le=365),
    team_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await analytics_svc.funnel(db, days=days, team_id=team_id)


@admin_router.get("/daily-active")
async def analytics_dau(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await analytics_svc.daily_active_users(db, days=days)


@admin_router.get("/teams")
async def analytics_teams(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return await analytics_svc.team_rollups(db, days=days)
