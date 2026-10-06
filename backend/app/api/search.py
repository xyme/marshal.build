"""Global search (beta-usability spec R2 / FSD B12).

One endpoint fanning out to the EXISTING list queries — projects, chat
sessions, marketplace samples — each behind the exact access seam its home
surface uses (nothing searchable that is not listable). Titles-first at this
tier: ILIKE, capped groups, no new indexes (the B1 pgvector work upgrades
sample search behind this same endpoint when it lands). Docs are searched
client-side over the static registry — they never touch the backend.

Spec-CONTENT search sits behind `feature_flags.global_search_spec_content`
(default OFF, admins included — search is a user surface, not a dark launch).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.tenant import tenant_scope
from app.models import Project, Spec, User

router = APIRouter(prefix="/search", tags=["search"])

GROUP_CAP = 5
MIN_QUERY_CHARS = 2


async def _visible_project_ids_stmt(db: AsyncSession, user: User):
    """The projects-list visibility clause (owned ∪ shared ∪ team), as a
    scalar subquery over live projects."""
    from app.services import collab
    from app.services import teams as teams_svc

    member_roles = await collab.member_project_ids(db, user.id)
    team_ids = list((await teams_svc.memberships_for_user(db, user.id)).keys())
    clauses = [Project.user_id == user.id]
    if member_roles:
        clauses.append(Project.id.in_(member_roles.keys()))
    if team_ids:
        clauses.append(Project.team_id.in_(team_ids))
    return tenant_scope(
        select(Project.id).where(
            or_(*clauses) if len(clauses) > 1 else clauses[0],
            Project.status != "deleted",
        ),
        Project,
    ).scalar_subquery()


@router.get("")
async def global_search(
    q: str = Query("", max_length=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = q.strip()
    groups: dict = {"projects": [], "sessions": [], "samples": []}
    if len(query) < MIN_QUERY_CHARS:
        return {"query": query, "groups": groups}
    like = f"%{query}%"

    # ---- projects: the list_projects construction + name ILIKE
    from app.services import collab
    from app.services import teams as teams_svc

    member_roles = await collab.member_project_ids(db, user.id)
    team_ids = list((await teams_svc.memberships_for_user(db, user.id)).keys())
    clauses = [Project.user_id == user.id]
    if member_roles:
        clauses.append(Project.id.in_(member_roles.keys()))
    if team_ids:
        clauses.append(Project.team_id.in_(team_ids))
    stmt = (
        tenant_scope(
            select(Project).where(
                or_(*clauses) if len(clauses) > 1 else clauses[0],
                Project.status != "deleted",
                Project.status != "archived",
                Project.name.ilike(like),
            ),
            Project,
        )
        .order_by(Project.updated_at.desc())
        .limit(GROUP_CAP)
    )
    groups["projects"] = [
        {"id": str(p.id), "name": p.name, "status": p.status}
        for p in (await db.execute(stmt)).scalars()
    ]

    # ---- sessions: the exact session-visibility clause, title ILIKE inside
    from app.services import chat as chat_service

    sessions = await chat_service.list_sessions(db, user, q=query)
    groups["sessions"] = [
        {"id": str(s.id), "title": s.title, "updated_at": s.updated_at.isoformat()}
        for s in sessions[:GROUP_CAP]
    ]

    # ---- marketplace samples: published-only + tenant scope come free
    from app.services import marketplace as marketplace_svc

    samples, _total = await marketplace_svc.list_samples(
        db, q=query, page_size=GROUP_CAP
    )
    groups["samples"] = [
        {
            "id": str(s.id),
            "title": s.title,
            "category": s.category,
            "complexity": s.complexity,
        }
        for s in samples
    ]

    # ---- spec content (flagged OFF by default — R2.3)
    from app.services.platform_settings import get_controls

    controls = await get_controls()
    if bool(controls.feature_flags.get("global_search_spec_content", False)):
        visible = await _visible_project_ids_stmt(db, user)
        rows = await db.execute(
            select(Spec)
            .where(Spec.project_id.in_(visible), Spec.content.ilike(like))
            .order_by(Spec.version.asc())
        )
        latest: dict[tuple[uuid.UUID, str], Spec] = {}
        for spec in rows.scalars():
            latest[(spec.project_id, spec.type)] = spec  # ascending → last wins
        specs = sorted(latest.values(), key=lambda s: s.created_at, reverse=True)[:GROUP_CAP]
        names: dict = {}
        if specs:
            name_rows = await db.execute(
                select(Project.id, Project.name).where(
                    Project.id.in_({s.project_id for s in specs})
                )
            )
            names = dict(name_rows.all())
        groups["specs"] = [
            {
                "project_id": str(s.project_id),
                "project_name": names.get(s.project_id),
                "type": s.type,
                "version": s.version,
            }
            for s in specs
        ]

    return {"query": query, "groups": groups}
