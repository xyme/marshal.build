"""Team workspaces (S15-02, owner decision D12: teams within one org).

Teams layer onto the existing project access seam rather than replacing it:
`collab.resolve_role` consults team membership AFTER ownership and explicit
project membership, and takes the HIGHEST role found. Consequences worth
knowing:

  - A team grant never lowers access someone already has.
  - Team `member` reads; team `lead` edits. Neither becomes owner — ownership
    transfer stays an explicit act (collaboration spec R4).
  - Personal projects (team_id NULL) behave exactly as before S15.

Team ≠ tenant. `tenant_id` remains the org boundary reserved for GA
multi-tenancy; see the migration note in f2a3b4c5d6e7.
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Team, TeamMember, User

logger = logging.getLogger("marshal.teams")

TEAM_ROLES = ("lead", "member")
# Team role → project role. Leads edit; members read. Deliberately conservative:
# a broad "everyone in the team can edit everything" default is how shared
# workspaces turn into accidental overwrites.
TEAM_ROLE_TO_PROJECT_ROLE = {"lead": "editor", "member": "viewer"}


class TeamError(Exception):
    """Team rule violation — surfaces as 422/409 at the API."""


# ------------------------------------------------------------------ queries


async def list_teams(db: AsyncSession, *, include_archived: bool = False) -> list[Team]:
    from app.core.tenant import tenant_scope

    stmt = tenant_scope(select(Team), Team)
    if not include_archived:
        stmt = stmt.where(Team.status == "active")
    return list((await db.execute(stmt.order_by(Team.name))).scalars())


async def memberships_for_user(db: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """{team_id: role} for one user — the input to access resolution."""
    rows = await db.execute(
        select(TeamMember.team_id, TeamMember.role).where(TeamMember.user_id == user_id)
    )
    return dict(rows.all())


async def team_role_for_project(
    db: AsyncSession, project: Project, user_id: uuid.UUID
) -> str | None:
    """Project role granted by team membership, or None."""
    if project.team_id is None:
        return None
    row = (
        await db.execute(
            select(TeamMember.role).where(
                TeamMember.team_id == project.team_id, TeamMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return TEAM_ROLE_TO_PROJECT_ROLE.get(row)


async def visible_team_ids(db: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await memberships_for_user(db, user_id)).keys())


async def members_with_users(db: AsyncSession, team_id: uuid.UUID) -> list[dict]:
    rows = await db.execute(
        select(TeamMember, User)
        .join(User, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team_id)
        .order_by(User.email)
    )
    return [
        {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name,
            "platform_role": user.role,
            "status": user.status,
            "team_role": member.role,
            "added_at": member.created_at,
        }
        for member, user in rows.all()
    ]


async def team_summary(db: AsyncSession, team: Team) -> dict:
    member_count = (
        await db.execute(
            select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id)
        )
    ).scalar_one()
    project_count = (
        await db.execute(
            select(func.count())
            .select_from(Project)
            .where(Project.team_id == team.id, Project.status != "deleted")
        )
    ).scalar_one()
    return {
        "id": str(team.id),
        "name": team.name,
        "description": team.description,
        "status": team.status,
        "budget_usd": float(team.budget_usd) if team.budget_usd is not None else None,
        "member_count": member_count,
        "project_count": project_count,
        "created_at": team.created_at,
    }


async def is_lead(db: AsyncSession, team_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    role = (
        await db.execute(
            select(TeamMember.role).where(
                TeamMember.team_id == team_id, TeamMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return role == "lead"


# ------------------------------------------------------------------ mutations


async def create_team(
    db: AsyncSession, *, name: str, description: str | None, actor: User
) -> Team:
    name = (name or "").strip()
    if not name:
        raise TeamError("Team name is required")
    team = Team(name=name, description=(description or None), created_by=actor.id)
    db.add(team)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise TeamError(f"A team named '{name}' already exists") from exc
    await db.refresh(team)
    logger.info("team created %s by %s", team.id, actor.email)
    return team


async def update_team(
    db: AsyncSession,
    team: Team,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    budget_usd: float | None = None,
    clear_budget: bool = False,
) -> Team:
    if name is not None:
        if not name.strip():
            raise TeamError("Team name is required")
        team.name = name.strip()
    if description is not None:
        team.description = description or None
    if status is not None:
        if status not in ("active", "archived"):
            raise TeamError("status must be 'active' or 'archived'")
        team.status = status
    # Cost-dashboard visibility budget (5 Sep 2026) — mirror of the per-user
    # override pattern: explicit clear flag, None means unchanged.
    if clear_budget:
        team.budget_usd = None
    elif budget_usd is not None:
        team.budget_usd = budget_usd
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise TeamError("A team with that name already exists") from exc
    await db.refresh(team)
    return team


async def set_members(
    db: AsyncSession, team: Team, entries: list[dict], actor: User
) -> list[dict]:
    """Replace the membership set. `entries` = [{user_id, role}].

    Declarative (replace, not patch) so the admin UI can send what it shows and
    removals cannot be silently lost.
    """
    wanted: dict[uuid.UUID, str] = {}
    for entry in entries:
        user_id = entry["user_id"]
        role = entry.get("role", "member")
        if role not in TEAM_ROLES:
            raise TeamError(f"Team role must be one of {', '.join(TEAM_ROLES)}")
        user = await db.get(User, user_id)
        if user is None:
            raise TeamError("Unknown user in membership list")
        wanted[user_id] = role

    existing = {
        member.user_id: member
        for member in (
            await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))
        ).scalars()
    }
    for user_id, role in wanted.items():
        member = existing.get(user_id)
        if member is None:
            db.add(
                TeamMember(
                    team_id=team.id, user_id=user_id, role=role, added_by=actor.id
                )
            )
        elif member.role != role:
            member.role = role
    for user_id, member in existing.items():
        if user_id not in wanted:
            await db.delete(member)
    await db.commit()
    logger.info("team %s membership set to %d member(s)", team.id, len(wanted))
    return await members_with_users(db, team.id)


async def remove_user_everywhere(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Drop all team memberships for a user (S15-03 offboarding). Returns count."""
    members = list(
        (
            await db.execute(select(TeamMember).where(TeamMember.user_id == user_id))
        ).scalars()
    )
    for member in members:
        await db.delete(member)
    if members:
        await db.commit()
    return len(members)


async def assign_project(
    db: AsyncSession, project: Project, team_id: uuid.UUID | None, actor: User
) -> Project:
    """Move a project into a team (or back to personal with None).

    Only the project OWNER or a platform admin may do this: it changes who can
    see the project, so it is an ownership-level decision rather than an
    editing one.
    """
    if project.user_id != actor.id and actor.role != "admin":
        raise TeamError("Only the project owner can change its team")
    if team_id is not None:
        team = await db.get(Team, team_id)
        if team is None or team.status != "active":
            raise TeamError("Team not found or archived")
        # Owners can only file a project into a team they belong to; admins may
        # place any project anywhere (they administer the org).
        if actor.role != "admin":
            memberships = await memberships_for_user(db, actor.id)
            if team_id not in memberships:
                raise TeamError("You can only assign projects to a team you belong to")
    project.team_id = team_id
    await db.commit()
    await db.refresh(project)
    return project
