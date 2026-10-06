"""Collaboration endpoints: members, transfer, comments, presence, project
sessions (collaboration spec R2/R4/R5/R6)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import project_editor, project_owner, project_viewer
from app.core.auth import get_current_user, require_admin_security_if_admin
from app.core.db import get_db
from app.models import ChatSession, Project, SpecComment, User
from app.schemas.chat import SessionOut
from app.schemas.projects import (
    CommentCreate,
    CommentReplyIn,
    MemberAdd,
    MemberPatch,
    PresenceIn,
    TransferIn,
)
from app.services import audit, collab
from app.services import notifications as notif

router = APIRouter(tags=["collaboration"])


def _err(exc: collab.CollabError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


# ------------------------------------------------------------------ members


@router.get("/projects/{project_id}/members")
async def list_members(
    request: Request,
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    owner = await db.get(User, project.user_id)
    return {
        "owner": {
            "user_id": str(project.user_id),
            "email": owner.email if owner else None,
            "name": owner.name if owner else None,
        },
        "members": await collab.list_members(db, project),
        "my_role": getattr(request.state, "project_role", None),
    }


@router.post("/projects/{project_id}/members", status_code=201)
async def add_member(
    payload: MemberAdd,
    request: Request,
    project: Project = Depends(project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        member, target, created = await collab.add_member(
            db, project, user, email=payload.email, role=payload.role
        )
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(
        request, member_email=target.email, role=payload.role, created=created
    )
    actor_name = user.name or user.email
    notif.emit(
        notif.emit_for_user(
            target.id,
            type="project_shared",
            title="Project shared with you",
            body=f'{actor_name} added you to "{project.name}" as {payload.role}.',
            link=f"/projects/{project.id}",
            dedupe_key=f"project_shared:{project.id}:{target.id}:{payload.role}",
        )
    )
    return {"user_id": str(target.id), "role": member.role, "created": created}


@router.patch("/projects/{project_id}/members/{user_id}")
async def change_member_role(
    user_id: uuid.UUID,
    payload: MemberPatch,
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        member = await collab.change_role(db, project, user_id, payload.role)
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(request, member_id=str(user_id), role=payload.role)
    notif.emit(
        notif.emit_for_user(
            user_id,
            type="project_role_changed",
            title="Your project role changed",
            body=f'You are now {payload.role} on "{project.name}".',
            link=f"/projects/{project.id}",
        )
    )
    return {"user_id": str(user_id), "role": member.role}


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
async def remove_member(
    user_id: uuid.UUID,
    request: Request,
    project: Project = Depends(project_owner),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await collab.remove_member(db, project, user_id)
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(request, member_id=str(user_id))
    notif.emit(
        notif.emit_for_user(
            user_id,
            type="project_unshared",
            title="Removed from a project",
            body=f'You no longer have access to "{project.name}".',
        )
    )


@router.post("/projects/{project_id}/transfer-ownership")
async def transfer_ownership(
    payload: TransferIn,
    request: Request,
    project: Project = Depends(project_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        old_owner_id, new_owner_id = await collab.transfer_ownership(
            db, project, payload.user_id
        )
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(
        request,
        before={"owner": str(old_owner_id)},
        after={"owner": str(new_owner_id)},
    )
    notif.emit(
        notif.emit_for_user(
            new_owner_id,
            type="ownership_transferred",
            title="You now own a project",
            body=f'Ownership of "{project.name}" was transferred to you.',
            link=f"/projects/{project.id}",
        )
    )
    notif.emit(
        notif.emit_for_user(
            old_owner_id,
            type="ownership_transferred",
            title="Ownership transferred",
            body=f'You transferred "{project.name}" and are now an editor on it.',
            link=f"/projects/{project.id}",
        )
    )
    return {"owner_id": str(new_owner_id), "previous_owner_id": str(old_owner_id)}


# ------------------------------------------------------------------ comments


@router.get("/projects/{project_id}/comments")
async def list_comments(
    doc_type: str | None = None,
    include_resolved: bool = False,
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    threads = await collab.list_comments(
        db, project, doc_type=doc_type, include_resolved=include_resolved
    )
    return {"threads": threads}


@router.post("/projects/{project_id}/comments", status_code=201)
async def create_comment(
    payload: CommentCreate,
    request: Request,
    project: Project = Depends(project_viewer),  # viewers may comment (R1.3)
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        comment = await collab.create_comment(
            db,
            project,
            user,
            doc_type=payload.doc_type,
            anchor=payload.anchor,
            anchor_text=payload.anchor_text,
            body=payload.body,
        )
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(
        request, resource_id=str(comment.id), doc_type=payload.doc_type, anchor=payload.anchor
    )
    return {"id": str(comment.id)}


async def _comment_access(
    comment_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> tuple[SpecComment, Project, str, User]:
    comment = await db.get(SpecComment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    resolved = await collab.resolve_role(db, comment.project_id, user)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    project, role = resolved
    request.state.project_role = role
    return comment, project, role, user


@router.post("/comments/{comment_id}/reply", status_code=201)
async def reply_comment(
    payload: CommentReplyIn,
    request: Request,
    ctx: tuple = Depends(_comment_access),
    db: AsyncSession = Depends(get_db),
) -> dict:
    comment, project, _role, user = ctx
    try:
        reply = await collab.reply_to_comment(db, comment, user, payload.body)
    except collab.CollabError as exc:
        raise _err(exc) from exc
    audit.set_audit_detail(
        request, resource_id=str(reply.id), project_id=str(project.id)
    )
    return {"id": str(reply.id)}


@router.post("/comments/{comment_id}/resolve")
async def resolve_comment(
    request: Request,
    ctx: tuple = Depends(_comment_access),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from datetime import UTC, datetime

    comment, project, role, user = ctx
    if not collab.can_moderate(comment, user, role):
        raise HTTPException(status_code=403, detail="Only the comment author, a project editor or the owner can resolve a comment")
    comment.resolved_at = datetime.now(UTC)
    comment.resolved_by = user.id
    await db.commit()
    audit.set_audit_detail(request, project_id=str(project.id))
    return {"id": str(comment.id), "resolved": True}


@router.post("/comments/{comment_id}/unresolve")
async def unresolve_comment(
    request: Request,
    ctx: tuple = Depends(_comment_access),
    db: AsyncSession = Depends(get_db),
) -> dict:
    comment, project, role, user = ctx
    if not collab.can_moderate(comment, user, role):
        raise HTTPException(status_code=403, detail="Only the comment author, a project editor or the owner can reopen a comment")
    comment.resolved_at = None
    comment.resolved_by = None
    await db.commit()
    audit.set_audit_detail(request, project_id=str(project.id))
    return {"id": str(comment.id), "resolved": False}


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    request: Request,
    ctx: tuple = Depends(_comment_access),
    db: AsyncSession = Depends(get_db),
) -> None:
    comment, project, role, user = ctx
    if not collab.can_delete(comment, user, role):
        raise HTTPException(status_code=403, detail="Only the comment author or the project owner can delete a comment")
    if comment.parent_id is None:
        # Root deletion removes replies (FK cascade needs explicit delete on sqlite)
        replies = await db.execute(
            select(SpecComment).where(SpecComment.parent_id == comment.id)
        )
        for reply in replies.scalars():
            await db.delete(reply)
    await db.delete(comment)
    await db.commit()
    audit.set_audit_detail(request, project_id=str(project.id))


# ------------------------------------------------------------------ presence


@router.post("/projects/{project_id}/presence")
async def presence_heartbeat(
    payload: PresenceIn,
    project: Project = Depends(project_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await collab.heartbeat(db, project, user, payload.surface)
    return {"active": await collab.active_presence(db, project)}


@router.get("/projects/{project_id}/presence")
async def presence_list(
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"active": await collab.active_presence(db, project)}


# ------------------------------------------------------------------ project sessions


@router.get("/projects/{project_id}/sessions", response_model=list[SessionOut])
async def project_sessions(
    project: Project = Depends(project_editor),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOut]:
    """Sessions attached to this project (editors; chat is an editing surface)."""
    rows = await db.execute(
        select(ChatSession)
        .where(ChatSession.project_id == project.id)
        .order_by(ChatSession.updated_at.desc())
    )
    sessions = list(rows.scalars())
    creator_ids = {s.user_id for s in sessions}
    names: dict[uuid.UUID, str] = {}
    if creator_ids:
        users = await db.execute(select(User).where(User.id.in_(creator_ids)))
        names = {u.id: (u.name or u.email.split("@")[0]) for u in users.scalars()}
    out = []
    for s in sessions:
        item = SessionOut.model_validate(s)
        item.is_mine = s.user_id == user.id
        item.creator_name = names.get(s.user_id)
        item.project_name = project.name
        out.append(item)
    return out
