"""Collaboration tests (collaboration spec) — authz matrix is the core."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AiRiskAssessment,
    ChatSession,
    Notification,
    Project,
    ProjectMember,
    ProjectPresence,
    ReviewerGroupMember,
    Spec,
    SpecComment,
    User,
)

pytestmark = pytest.mark.asyncio


async def _mk_user(db, *, email: str, role="power", persona="power") -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        persona=persona,
        onboarding_completed=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _mk_project(db, owner: User, *, name="Shared Project") -> Project:
    project = Project(user_id=owner.id, name=name, status="draft")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _add_member(db, project: Project, user: User, role: str) -> None:
    db.add(ProjectMember(project_id=project.id, user_id=user.id, role=role, added_by=project.user_id))
    await db.commit()


async def _mk_spec(db, project: Project, user: User, content="# Req\n\n## Section A\nBody") -> Spec:
    spec = Spec(project_id=project.id, version=1, type="requirements", content=content, created_by=user.id)
    db.add(spec)
    await db.commit()
    return spec


# ------------------------------------------------------------- authz matrix


async def test_role_matrix_read_and_write(db_session, test_user, client_for):
    owner = test_user
    editor = await _mk_user(db_session, email="editor@marshal.demo")
    viewer = await _mk_user(db_session, email="viewer@marshal.demo")
    outsider = await _mk_user(db_session, email="outsider@marshal.demo")
    admin = await _mk_user(db_session, email="root@marshal.demo", role="admin")
    await _mk_user(db_session, email="temp@marshal.demo")  # owner's add-member target
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, editor, "editor")
    await _add_member(db_session, project, viewer, "viewer")
    await _mk_spec(db_session, project, owner)

    # (user, GET project, PUT project, save spec, PUT budget, POST members)
    cases = [
        (owner, 200, 200, 200, 200, "member_ok"),
        (editor, 200, 403, 200, 403, 403),
        (viewer, 200, 403, 403, 403, 403),
        (outsider, 404, 404, 404, 404, 404),
        (admin, 404, 404, 404, 404, 404),  # no implicit admin access (R1.2)
    ]
    for user, get_s, put_s, save_s, budget_s, members_s in cases:
        async with client_for(user) as client:
            r = await client.get(f"/api/v1/projects/{project.id}")
            assert r.status_code == get_s, (user.email, "GET", r.status_code)
            r = await client.put(f"/api/v1/projects/{project.id}", json={"name": "Renamed?"})
            assert r.status_code == put_s, (user.email, "PUT", r.status_code)
            r = await client.put(
                f"/api/v1/projects/{project.id}/specs/requirements",
                json={"content": f"# Req by {user.email}"},
            )
            assert r.status_code == save_s, (user.email, "SAVE", r.status_code)
            r = await client.put(
                f"/api/v1/projects/{project.id}/budget", json={"budget_override_usd": 10}
            )
            assert r.status_code == budget_s, (user.email, "BUDGET", r.status_code)
            r = await client.post(
                f"/api/v1/projects/{project.id}/members",
                json={"email": "temp@marshal.demo", "role": "viewer"},
            )
            if members_s == "member_ok":
                assert r.status_code == 201, r.text
            else:
                assert r.status_code == members_s, (user.email, "MEMBERS", r.status_code)


async def test_owner_rename_kept_owner_gated_regression(db_session, test_user, client_for):
    """PUT rename by owner still works after the seam swap (degenerate case)."""
    project = await _mk_project(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.put(f"/api/v1/projects/{project.id}", json={"name": "New Name"})
        assert r.status_code == 200 and r.json()["name"] == "New Name"


# ------------------------------------------------------------- members CRUD


async def test_member_lifecycle_and_notifications(db_session, test_user, client_for, audit_db):
    owner = test_user
    friend = await _mk_user(db_session, email="friend@marshal.demo")
    project = await _mk_project(db_session, owner)
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/members",
            json={"email": "nobody@marshal.demo", "role": "viewer"},
        )
        assert r.status_code == 422  # unknown email, safe copy

        r = await client.post(
            f"/api/v1/projects/{project.id}/members",
            json={"email": friend.email, "role": "viewer"},
        )
        assert r.status_code == 201 and r.json()["created"] is True

        # duplicate → role upsert, not error
        r = await client.post(
            f"/api/v1/projects/{project.id}/members",
            json={"email": friend.email, "role": "editor"},
        )
        assert r.status_code == 201 and r.json()["created"] is False
        assert r.json()["role"] == "editor"

        # owner cannot be added as member
        r = await client.post(
            f"/api/v1/projects/{project.id}/members",
            json={"email": owner.email, "role": "viewer"},
        )
        assert r.status_code == 422

        r = await client.get(f"/api/v1/projects/{project.id}/members")
        data = r.json()
        assert data["owner"]["email"] == owner.email
        assert [m["role"] for m in data["members"]] == ["editor"]

        r = await client.patch(
            f"/api/v1/projects/{project.id}/members/{friend.id}", json={"role": "viewer"}
        )
        assert r.status_code == 200 and r.json()["role"] == "viewer"

        r = await client.delete(f"/api/v1/projects/{project.id}/members/{friend.id}")
        assert r.status_code == 204

    await asyncio.sleep(0.05)  # let notification tasks settle
    rows = (
        (await db_session.execute(select(Notification).where(Notification.user_id == friend.id)))
        .scalars()
        .all()
    )
    types = {n.type for n in rows}
    assert "project_shared" in types
    assert "project_role_changed" in types
    assert "project_unshared" in types


async def test_shared_projects_in_list_with_role(db_session, test_user, client_for):
    owner = test_user
    editor = await _mk_user(db_session, email="lister@marshal.demo")
    project = await _mk_project(db_session, owner, name="Owner Project")
    await _add_member(db_session, project, editor, "editor")
    async with client_for(editor) as client:
        r = await client.get("/api/v1/projects")
        items = r.json()["items"]
        mine = next(i for i in items if i["id"] == str(project.id))
        assert mine["my_role"] == "editor"
    async with client_for(owner) as client:
        r = await client.get("/api/v1/projects")
        assert r.json()["items"][0]["my_role"] == "owner"


# ------------------------------------------------------------- transfer


async def test_ownership_transfer(db_session, test_user, client_for):
    owner = test_user
    successor = await _mk_user(db_session, email="successor@marshal.demo")
    outsider = await _mk_user(db_session, email="stranger@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, successor, "editor")
    async with client_for(owner) as client:
        # non-member target refused
        r = await client.post(
            f"/api/v1/projects/{project.id}/transfer-ownership",
            json={"user_id": str(outsider.id)},
        )
        assert r.status_code == 422
        r = await client.post(
            f"/api/v1/projects/{project.id}/transfer-ownership",
            json={"user_id": str(successor.id)},
        )
        assert r.status_code == 200

    await db_session.refresh(project)
    assert project.user_id == successor.id
    member_rows = (
        (
            await db_session.execute(
                select(ProjectMember).where(ProjectMember.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert {(m.user_id, m.role) for m in member_rows} == {(owner.id, "editor")}

    # rights flipped: old owner can edit specs but not manage members
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/members",
            json={"email": "stranger@marshal.demo", "role": "viewer"},
        )
        assert r.status_code == 403
    async with client_for(successor) as client:
        r = await client.put(
            f"/api/v1/projects/{project.id}/budget", json={"budget_override_usd": 5}
        )
        assert r.status_code == 200


# ------------------------------------------------------------- comments


async def test_comment_threads_rules(db_session, test_user, client_for):
    owner = test_user
    viewer = await _mk_user(db_session, email="commenter@marshal.demo")
    other_viewer = await _mk_user(db_session, email="lurker@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, viewer, "viewer")
    await _add_member(db_session, project, other_viewer, "viewer")
    await _mk_spec(db_session, project, owner)

    async with client_for(viewer) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/comments",
            json={
                "doc_type": "requirements",
                "anchor": "section-a",
                "anchor_text": "Section A",
                "body": "Should this cover exports too?",
            },
        )
        assert r.status_code == 201, r.text
        root_id = r.json()["id"]

    async with client_for(owner) as client:
        r = await client.post(f"/api/v1/comments/{root_id}/reply", json={"body": "Yes — S4 zip."})
        assert r.status_code == 201
        reply_id = r.json()["id"]
        # 1-level threading: replying to a reply is refused
        r = await client.post(f"/api/v1/comments/{reply_id}/reply", json={"body": "nested?"})
        assert r.status_code == 422

    # non-author viewer cannot resolve; author can
    async with client_for(other_viewer) as client:
        r = await client.post(f"/api/v1/comments/{root_id}/resolve")
        assert r.status_code == 403
    async with client_for(viewer) as client:
        r = await client.post(f"/api/v1/comments/{root_id}/resolve")
        assert r.status_code == 200

    # resolved threads hidden by default, visible with include_resolved
    async with client_for(owner) as client:
        r = await client.get(f"/api/v1/projects/{project.id}/comments?doc_type=requirements")
        assert r.json()["threads"] == []
        r = await client.get(
            f"/api/v1/projects/{project.id}/comments?doc_type=requirements&include_resolved=true"
        )
        threads = r.json()["threads"]
        assert len(threads) == 1 and threads[0]["resolved"] is True
        assert len(threads[0]["replies"]) == 1

    # reply notification went to the thread author (owner replied to viewer)
    await asyncio.sleep(0.05)
    notif_types = {
        n.type
        for n in (
            await db_session.execute(
                select(Notification).where(Notification.user_id == viewer.id)
            )
        ).scalars()
    }
    assert "comment_reply" in notif_types


async def test_comment_delete_cascade_and_moderation(db_session, test_user, client_for):
    owner = test_user
    author = await _mk_user(db_session, email="author@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, author, "viewer")

    async with client_for(author) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/comments",
            json={"doc_type": "design", "anchor": "x", "anchor_text": "X", "body": "root"},
        )
        root_id = r.json()["id"]
        r = await client.post(f"/api/v1/comments/{root_id}/reply", json={"body": "self reply"})
        assert r.status_code == 201

    # owner moderation delete of someone else's root removes replies
    async with client_for(owner) as client:
        r = await client.delete(f"/api/v1/comments/{root_id}")
        assert r.status_code == 204
    remaining = (
        (await db_session.execute(select(SpecComment))).scalars().all()
    )
    assert remaining == []


# ------------------------------------------------------------- presence


async def test_presence_heartbeat_and_staleness(db_session, test_user, client_for):
    owner = test_user
    buddy = await _mk_user(db_session, email="buddy@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, buddy, "viewer")

    async with client_for(buddy) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/presence", json={"surface": "spec"}
        )
        assert r.status_code == 200
        active = r.json()["active"]
        assert [a["email"] for a in active] == [buddy.email]

    # stale rows drop out of the active list
    row = await db_session.get(ProjectPresence, (project.id, buddy.id))
    row.last_seen_at = datetime.now(UTC) - timedelta(seconds=300)
    await db_session.commit()
    async with client_for(owner) as client:
        r = await client.get(f"/api/v1/projects/{project.id}/presence")
        assert r.json()["active"] == []


# ------------------------------------------------------------- sessions


async def test_shared_sessions_access_and_rules(db_session, test_user, client_for, monkeypatch):
    async def _no_dynamo(session_id):
        return None

    monkeypatch.setattr("app.services.chat.delete_messages", _no_dynamo)
    owner = test_user
    editor = await _mk_user(db_session, email="coeditor@marshal.demo")
    viewer = await _mk_user(db_session, email="watcher@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, editor, "editor")
    await _add_member(db_session, project, viewer, "viewer")

    session = ChatSession(user_id=owner.id, project_id=project.id, title="Owner session")
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    async with client_for(editor) as client:
        r = await client.get(f"/api/v1/chat/sessions/{session.id}")
        assert r.status_code == 200
        # appears in the editor's session list with creator attribution
        r = await client.get("/api/v1/chat/sessions")
        items = r.json()
        shared = next(i for i in items if i["id"] == str(session.id))
        assert shared["is_mine"] is False
        assert shared["creator_name"]
        assert shared["project_name"] == project.name
        # editors may start new sessions on the project
        r = await client.post("/api/v1/chat/sessions", json={"project_id": str(project.id)})
        assert r.status_code == 201
        assert r.json()["project_id"] == str(project.id)
        # ... but not delete someone else's transcript
        r = await client.delete(f"/api/v1/chat/sessions/{session.id}")
        assert r.status_code == 403
        # project sessions listing for editors
        r = await client.get(f"/api/v1/projects/{project.id}/sessions")
        assert r.status_code == 200 and len(r.json()) == 2

    async with client_for(viewer) as client:
        r = await client.get(f"/api/v1/chat/sessions/{session.id}")
        assert r.status_code == 404  # chat is an editing surface
        r = await client.post("/api/v1/chat/sessions", json={"project_id": str(project.id)})
        assert r.status_code == 422
        r = await client.get(f"/api/v1/projects/{project.id}/sessions")
        assert r.status_code == 403

    # project owner may delete any session on the project
    async with client_for(owner) as client:
        r = await client.delete(f"/api/v1/chat/sessions/{session.id}")
        assert r.status_code == 204


async def test_editor_invocation_charges_acting_user(db_session, test_user, client_for, monkeypatch):
    """R3.3: the ACTING user's cap is charged on shared projects."""
    owner = test_user
    editor = await _mk_user(db_session, email="payer@marshal.demo")
    project = await _mk_project(db_session, owner)
    await _add_member(db_session, project, editor, "editor")
    session = ChatSession(user_id=owner.id, project_id=project.id, title="Shared chat")
    db_session.add(session)
    await db_session.commit()

    captured = {}

    async def fake_preflight(ctx, streaming=False):
        captured["user_id"] = ctx.user_id
        captured["project_id"] = ctx.project_id
        from app.services.ratelimit import RateLimited

        raise RateLimited(scope="user", retry_after_s=7)

    monkeypatch.setattr("app.api.chat.preflight_model_call", fake_preflight)
    async with client_for(editor) as client:
        r = await client.post(
            f"/api/v1/chat/sessions/{session.id}/messages", json={"content": "hello"}
        )
        assert r.status_code == 429  # short-circuited pre-stream
    assert captured["user_id"] == editor.id  # acting user, NOT the owner
    assert captured["project_id"] == project.id


# ------------------------------------------------------------- governance viewer


async def test_reviewer_gets_temporary_viewer_access(db_session, test_user, client_for):
    owner = test_user
    reviewer = await _mk_user(db_session, email="reviewer@marshal.demo")
    admin = await _mk_user(db_session, email="chief@marshal.demo", role="admin")
    project = await _mk_project(db_session, owner)
    await _mk_spec(db_session, project, owner)
    db_session.add(
        ReviewerGroupMember(group_name="managers", user_id=reviewer.id, added_by=admin.id)
    )
    assessment = AiRiskAssessment(
        project_id=project.id,
        content_hash="h1",
        score=55,
        level="medium",
        decision="pending",
        assigned_group="managers",
        routed_at=datetime.now(UTC),
    )
    db_session.add(assessment)
    await db_session.commit()

    # Pending + routed to their group → read access (the S6 "View spec" link works)
    for user in (reviewer, admin):
        async with client_for(user) as client:
            r = await client.get(f"/api/v1/projects/{project.id}")
            assert r.status_code == 200, user.email
            assert r.json()["my_role"] == "viewer"
            # read-only: no spec writes
            r = await client.put(
                f"/api/v1/projects/{project.id}/specs/requirements", json={"content": "# nope"}
            )
            assert r.status_code == 403

    # Decision made → the grant evaporates
    assessment.decision = "approved"
    await db_session.commit()
    for user in (reviewer, admin):
        async with client_for(user) as client:
            r = await client.get(f"/api/v1/projects/{project.id}")
            assert r.status_code == 404, user.email
