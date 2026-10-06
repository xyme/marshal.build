"""In-house usage analytics (S16-04, product decision D6: no third party).

Producers call `track()` fire-and-forget at six seams — sign-in, chat message,
spec save, build start, deploy start, teardown — the same never-break-the-caller
discipline as audit and notifications. Everything is server-side: no client
beacons, no request-level tracking, no third-party SDK (the privacy note in the
docs states exactly this).

The funnel reads deliberately count DISTINCT USERS per step, not raw event
volume: "40 people chatted, 12 deployed" is the adoption question S16-04 asks;
volumes are reported separately.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Team, UsageEvent, User

logger = logging.getLogger("marshal.analytics")

# Funnel order (S16-04 AC). Teardown completes the lifecycle story: a team that
# tears down its Enclaves is USING the governance model, not abandoning it.
FUNNEL_STEPS = [
    "sign_in",
    "chat_message",
    "spec_saved",
    "build_started",
    "deploy_started",
    "teardown",
]


async def record(
    user_id: uuid.UUID,
    *,
    event: str,
    project_id: uuid.UUID | None = None,
    detail: dict | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Write one event on a fresh session. Never raises to the caller's flow.

    team_id is denormalized from the project AT WRITE TIME so per-team rollups
    reflect where the work actually happened, not where a project moved later.
    """
    from app.core.db import SessionLocal

    try:
        async with SessionLocal() as db:
            team_id = None
            if project_id is not None:
                team_id = (
                    await db.execute(select(Project.team_id).where(Project.id == project_id))
                ).scalar_one_or_none()
            db.add(
                UsageEvent(
                    user_id=user_id,
                    event=event,
                    project_id=project_id,
                    team_id=team_id,
                    detail=detail or {},
                    dedupe_key=dedupe_key,
                )
            )
            await db.commit()
    except IntegrityError:
        pass  # dedupe hit — by design (e.g. second sign_in of the day)
    except Exception:  # noqa: BLE001 — analytics must never break the caller
        logger.exception("usage event write failed (event=%s)", event)


def track(
    user_id: uuid.UUID,
    event: str,
    *,
    project_id: uuid.UUID | None = None,
    detail: dict | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Fire-and-forget scheduling; silently a no-op without a running loop."""
    coro = record(
        user_id, event=event, project_id=project_id, detail=detail, dedupe_key=dedupe_key
    )
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # pragma: no cover — scripts without a loop
        coro.close()


def track_sign_in(user_id: uuid.UUID) -> None:
    """One sign_in row per user per UTC day (dedupe key), fed by the /users/me
    bootstrap call every page load makes — cheap and beacon-free."""
    today = datetime.now(UTC).date().isoformat()
    track(user_id, "sign_in", dedupe_key=f"signin:{user_id}:{today}")


# ------------------------------------------------------------------- reads


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, min(days, 365)))


async def funnel(db: AsyncSession, *, days: int = 30, team_id: uuid.UUID | None = None) -> dict:
    """Distinct users per funnel step + conversion vs the step before."""
    since = _since(days)
    stmt = (
        select(UsageEvent.event, func.count(func.distinct(UsageEvent.user_id)), func.count())
        .where(UsageEvent.created_at >= since, UsageEvent.event.in_(FUNNEL_STEPS))
        .group_by(UsageEvent.event)
    )
    if team_id is not None:
        stmt = stmt.where(UsageEvent.team_id == team_id)
    rows = {event: (users, events) for event, users, events in (await db.execute(stmt)).all()}

    steps = []
    prev_users: int | None = None
    for step in FUNNEL_STEPS:
        users, events = rows.get(step, (0, 0))
        conversion = None
        if prev_users is not None:
            conversion = round(users / prev_users, 3) if prev_users else 0.0
        steps.append(
            {"step": step, "users": users, "events": events, "conversion": conversion}
        )
        prev_users = users
    return {"days": days, "team_id": str(team_id) if team_id else None, "steps": steps}


async def daily_active_users(db: AsyncSession, *, days: int = 30) -> list[dict]:
    """DAU series from sign_in rows (already one per user per day)."""
    since = _since(days)
    rows = await db.execute(
        select(UsageEvent.created_at).where(
            UsageEvent.event == "sign_in", UsageEvent.created_at >= since
        )
    )
    buckets: dict[str, int] = {}
    for (created_at,) in rows.all():
        buckets[created_at.date().isoformat()] = buckets.get(created_at.date().isoformat(), 0) + 1
    start = since.date()
    today = datetime.now(UTC).date()
    series = []
    day = start
    while day <= today:
        key = day.isoformat()
        series.append({"date": key, "users": buckets.get(key, 0)})
        day += timedelta(days=1)
    return series


async def team_rollups(db: AsyncSession, *, days: int = 30) -> list[dict]:
    """Per-team activity: active users + step volumes (S16-04 AC)."""
    since = _since(days)
    rows = (
        await db.execute(
            select(
                UsageEvent.team_id,
                UsageEvent.event,
                func.count(func.distinct(UsageEvent.user_id)),
                func.count(),
            )
            .where(UsageEvent.created_at >= since)
            .group_by(UsageEvent.team_id, UsageEvent.event)
        )
    ).all()
    names: dict[uuid.UUID, str] = dict(
        (await db.execute(select(Team.id, Team.name))).all()
    )
    teams: dict[uuid.UUID | None, dict] = {}
    for team_id, event, users, events in rows:
        entry = teams.setdefault(
            team_id,
            {
                "team_id": str(team_id) if team_id else None,
                "team": names.get(team_id, "(personal)") if team_id else "(personal)",
                "active_users": 0,
                "events": {},
            },
        )
        entry["events"][event] = {"users": users, "count": events}
        entry["active_users"] = max(entry["active_users"], users)
    return sorted(teams.values(), key=lambda t: -t["active_users"])


async def summary(db: AsyncSession, *, days: int = 30) -> dict:
    """Headline numbers for the admin dashboard."""
    since = _since(days)
    total_events = (
        await db.execute(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.created_at >= since)
        )
    ).scalar_one()
    active = (
        await db.execute(
            select(func.count(func.distinct(UsageEvent.user_id))).where(
                UsageEvent.created_at >= since
            )
        )
    ).scalar_one()
    registered = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    return {
        "days": days,
        "active_users": active,
        "registered_users": registered,
        "events": total_events,
    }
