"""Project collaboration: access resolution seam, membership, transfer,
comments, presence (collaboration spec).

THE access rule for project-scoped surfaces lives here (spec R1): every
endpoint declares a minimum role via `app.api.deps.require_project_role`,
which delegates to `resolve_role`. Roles: owner ⊃ editor ⊃ viewer.

Admins do NOT get implicit membership (privacy stance, R1.2) — with one
governance carve-out: while a project has a PENDING risk assessment, its
eligible deciders (assigned/escalated group members, and admins — mirroring
S6 `can_decide`) resolve as read-only viewers so the §4.6.4 review card's
"View spec" link works. This intentionally NARROWS the pre-S7 blanket admin
bypass and FIXES the S6 gap where non-admin reviewers 404'd on that link.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiRiskAssessment,
    Project,
    ProjectMember,
    ProjectPresence,
    SpecComment,
    User,
)

logger = logging.getLogger("marshal.collab")

ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}
MEMBER_ROLES = ("viewer", "editor")
PRESENCE_STALE_S = 90
PRESENCE_PRUNE_H = 24


class CollabError(Exception):
    """Membership/comment rule violation — surfaces as 422/409 at the API."""


# ------------------------------------------------------------------ access seam


async def _governance_viewer(db: AsyncSession, project_id: uuid.UUID, user: User) -> bool:
    """Read-only access for reviewers of a pending assessment (see module doc)."""
    result = await db.execute(
        select(AiRiskAssessment)
        .where(
            AiRiskAssessment.project_id == project_id,
            AiRiskAssessment.decision == "pending",
        )
        .order_by(AiRiskAssessment.created_at.desc())
        .limit(1)
    )
    assessment = result.scalar_one_or_none()
    if assessment is None:
        return False
    if user.role == "admin":
        return True
    from app.services.risk_review import user_group_memberships

    groups = set(await user_group_memberships(db, user.id))
    eligible = {g for g in (assessment.assigned_group, assessment.escalated_to) if g}
    return bool(groups & eligible)


async def resolve_role(
    db: AsyncSession, project_id: uuid.UUID, user: User
) -> tuple[Project, str] | None:
    """Return (project, role) or None when the project is invisible to the user."""
    from app.core.tenant import tenant_scope

    project = (
        await db.execute(
            tenant_scope(
                select(Project).where(Project.id == project_id, Project.status != "deleted"),
                Project,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        return None
    if project.user_id == user.id:
        return project, "owner"
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    # S15-02: team membership grants access to the team's projects. Highest
    # role wins, so an explicit project grant is never lowered by a team one.
    from app.services import teams as teams_svc

    team_role = await teams_svc.team_role_for_project(db, project, user.id)
    candidates = [role for role in (member.role if member else None, team_role) if role]
    if candidates:
        return project, max(candidates, key=lambda role: ROLE_ORDER.get(role, -1))
    if await _governance_viewer(db, project_id, user):
        return project, "viewer"
    return None


def role_atleast(role: str, min_role: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER[min_role]


async def member_project_ids(db: AsyncSession, user_id: uuid.UUID) -> dict[uuid.UUID, str]:
    rows = await db.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.user_id == user_id
        )
    )
    return dict(rows.all())


# ------------------------------------------------------------------ membership


async def list_members(db: AsyncSession, project: Project) -> list[dict]:
    rows = await db.execute(
        select(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.created_at.asc())
    )
    return [
        {
            "user_id": str(member.user_id),
            "email": user.email,
            "name": user.name,
            "role": member.role,
            "added_at": member.created_at.isoformat(),
        }
        for member, user in rows.all()
    ]


async def add_member(
    db: AsyncSession, project: Project, actor: User, *, email: str, role: str
) -> tuple[ProjectMember, User, bool]:
    """Add (or role-upsert) a member by email. Returns (row, user, created)."""
    if role not in MEMBER_ROLES:
        raise CollabError(f"Role must be one of: {', '.join(MEMBER_ROLES)}")
    target = (
        await db.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if target is None or target.status != "active":
        # 404-safe copy: do not disclose which addresses have accounts beyond
        # this project-share context (single-tenant, low sensitivity — spec R2.1)
        raise CollabError("No active user with that email — they need to sign in once first")
    if target.id == project.user_id:
        raise CollabError("That user owns this project")
    existing = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.user_id == target.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = role
        await db.commit()
        await db.refresh(existing)
        return existing, target, False
    member = ProjectMember(
        project_id=project.id, user_id=target.id, role=role, added_by=actor.id
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member, target, True


async def change_role(
    db: AsyncSession, project: Project, user_id: uuid.UUID, role: str
) -> ProjectMember:
    if role not in MEMBER_ROLES:
        raise CollabError(f"Role must be one of: {', '.join(MEMBER_ROLES)}")
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise CollabError("Not a member of this project")
    member.role = role
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(db: AsyncSession, project: Project, user_id: uuid.UUID) -> None:
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id, ProjectMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise CollabError("Not a member of this project")
    await db.delete(member)
    # Presence row too — they no longer belong on the avatar strip
    await db.execute(
        delete(ProjectPresence).where(
            ProjectPresence.project_id == project.id, ProjectPresence.user_id == user_id
        )
    )
    await db.commit()


async def transfer_ownership(
    db: AsyncSession, project: Project, new_owner_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Old owner → editor member; target member row removed; projects.user_id
    updated — one transaction (spec R5.1). Returns (old_owner_id, new_owner_id)."""
    member = (
        await db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == new_owner_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise CollabError("Ownership can only transfer to an existing member")
    old_owner_id = project.user_id
    await db.delete(member)
    db.add(
        ProjectMember(
            project_id=project.id,
            user_id=old_owner_id,
            role="editor",
            added_by=new_owner_id,
        )
    )
    project.user_id = new_owner_id
    await db.commit()
    await db.refresh(project)
    return old_owner_id, new_owner_id


# ------------------------------------------------------------------ comments

DOC_TYPES = ("requirements", "design", "tasks")


async def create_comment(
    db: AsyncSession,
    project: Project,
    author: User,
    *,
    doc_type: str,
    anchor: str,
    anchor_text: str,
    body: str,
) -> SpecComment:
    if doc_type not in DOC_TYPES:
        raise CollabError(f"Unknown document type: {doc_type}")
    comment = SpecComment(
        project_id=project.id,
        doc_type=doc_type,
        anchor=anchor.strip()[:256],
        anchor_text=anchor_text.strip()[:512],
        author_id=author.id,
        body=body,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


async def reply_to_comment(
    db: AsyncSession, parent: SpecComment, author: User, body: str
) -> SpecComment:
    if parent.parent_id is not None:
        raise CollabError("Replies cannot be nested further (one level)")
    reply = SpecComment(
        project_id=parent.project_id,
        doc_type=parent.doc_type,
        anchor=parent.anchor,
        anchor_text=parent.anchor_text,
        parent_id=parent.id,
        author_id=author.id,
        body=body,
    )
    db.add(reply)
    await db.commit()
    await db.refresh(reply)

    # Notify the parent author (skip self-reply) — collaboration spec R4.5
    if parent.author_id != author.id:
        from app.services import notifications as notif

        project_id = parent.project_id
        parent_author_id = parent.author_id
        actor_name = author.name or author.email

        async def send() -> None:
            await notif.emit_for_user(
                parent_author_id,
                type="comment_reply",
                title="New reply to your comment",
                body=f"{actor_name} replied in a spec discussion.",
                link=f"/projects/{project_id}?tab=spec",
            )

        notif.emit(send())
    return reply


def can_moderate(comment: SpecComment, user: User, role: str) -> bool:
    """Resolve/unresolve rights: author, editors, owner (spec R4.3)."""
    return comment.author_id == user.id or role_atleast(role, "editor")


def can_delete(comment: SpecComment, user: User, role: str) -> bool:
    """Delete rights: author, or owner as moderation (spec R4.4)."""
    return comment.author_id == user.id or role == "owner"


async def list_comments(
    db: AsyncSession,
    project: Project,
    *,
    doc_type: str | None = None,
    include_resolved: bool = False,
) -> list[dict]:
    stmt = select(SpecComment, User).join(User, SpecComment.author_id == User.id).where(
        SpecComment.project_id == project.id
    )
    if doc_type:
        stmt = stmt.where(SpecComment.doc_type == doc_type)
    stmt = stmt.order_by(SpecComment.created_at.asc())
    rows = (await db.execute(stmt)).all()

    def out(comment: SpecComment, user: User) -> dict:
        return {
            "id": str(comment.id),
            "doc_type": comment.doc_type,
            "anchor": comment.anchor,
            "anchor_text": comment.anchor_text,
            "parent_id": str(comment.parent_id) if comment.parent_id else None,
            "author_id": str(comment.author_id),
            "author_name": user.name or user.email.split("@")[0],
            "body": comment.body,
            "resolved": comment.resolved_at is not None,
            "created_at": comment.created_at.isoformat(),
            "replies": [],
        }

    roots: dict[str, dict] = {}
    replies: list[dict] = []
    for comment, user in rows:
        item = out(comment, user)
        if comment.parent_id is None:
            roots[item["id"]] = item
        else:
            replies.append(item)
    for reply in replies:
        parent = roots.get(reply["parent_id"])
        if parent is not None:
            parent["replies"].append(reply)
    threads = list(roots.values())
    if not include_resolved:
        threads = [t for t in threads if not t["resolved"]]
    return threads


# ------------------------------------------------------------------ presence


async def heartbeat(
    db: AsyncSession, project: Project, user: User, surface: str
) -> None:
    now = datetime.now(UTC)
    row = await db.get(ProjectPresence, (project.id, user.id))
    if row is None:
        db.add(
            ProjectPresence(
                project_id=project.id, user_id=user.id,
                surface=surface[:32], last_seen_at=now,
            )
        )
    else:
        row.surface = surface[:32]
        row.last_seen_at = now
    # Lazy prune piggybacks on the heartbeat (spec R6.3 — no scheduler)
    await db.execute(
        delete(ProjectPresence).where(
            ProjectPresence.last_seen_at < now - timedelta(hours=PRESENCE_PRUNE_H)
        )
    )
    await db.commit()


async def active_presence(db: AsyncSession, project: Project) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(seconds=PRESENCE_STALE_S)
    rows = await db.execute(
        select(ProjectPresence, User)
        .join(User, ProjectPresence.user_id == User.id)
        .where(ProjectPresence.project_id == project.id)
    )
    items = []
    for presence, user in rows.all():
        seen = presence.last_seen_at
        if seen.tzinfo is None:  # sqlite returns naive datetimes
            seen = seen.replace(tzinfo=UTC)
        if seen < cutoff:
            continue
        items.append(
            {
                "user_id": str(presence.user_id),
                "name": user.name or user.email.split("@")[0],
                "email": user.email,
                "surface": presence.surface,
                "last_seen_at": seen.isoformat(),
            }
        )
    return items
