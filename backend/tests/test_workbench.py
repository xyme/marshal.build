"""Home workbench aggregate (beta-usability spec R1 / FSD B14): every section
shows exactly what its home surface would — no more, no less."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    AiRiskAssessment,
    Alert,
    CodegenBuild,
    Deployment,
    Project,
    ProjectMember,
    ReviewerGroupMember,
    User,
)

pytestmark = pytest.mark.asyncio


async def _project(db, owner, name="P", status="spec_complete") -> Project:
    project = Project(user_id=owner.id, name=name, status=status)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _other_user(db) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:10]}",
        email=f"other-{uuid.uuid4().hex[:6]}@marshal.demo",
        role="power",
        persona="power",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _fetch(client) -> dict:
    r = await client.get("/api/v1/users/me/workbench")
    assert r.status_code == 200, r.text
    return r.json()


async def test_empty_workbench(test_user, client_for):
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert data["reviews"] == {"is_reviewer": False, "pending": 0}
        assert data["expiring_deployments"] == []
        assert data["failed_builds"] == []
        assert data["awaiting_resubmission"] == []
        assert "ops" not in data  # non-admin never sees the ops line (R1.4)


async def test_expiring_deployments_window(db_session, test_user, client_for):
    now = datetime.now(UTC)
    soon = await _project(db_session, test_user, "Soon")
    later = await _project(db_session, test_user, "Later")
    forever = await _project(db_session, test_user, "Forever")
    db_session.add_all(
        [
            Deployment(
                project_id=soon.id, user_id=test_user.id, status="active",
                expires_at=now + timedelta(hours=12), app_url="https://soon.example",
            ),
            Deployment(  # outside the 24h window
                project_id=later.id, user_id=test_user.id, status="active",
                expires_at=now + timedelta(hours=48),
            ),
            Deployment(  # grandfathered never-expires
                project_id=forever.id, user_id=test_user.id, status="active",
                expires_at=None,
            ),
        ]
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert [d["project_name"] for d in data["expiring_deployments"]] == ["Soon"]
        assert 0 < data["expiring_deployments"][0]["hours_left"] <= 24


async def test_expiring_is_owner_scoped(db_session, test_user, client_for):
    """Another user's expiring deployment never shows (the ask is theirs)."""
    other = await _other_user(db_session)
    theirs = await _project(db_session, other, "Theirs")
    db_session.add(
        Deployment(
            project_id=theirs.id, user_id=other.id, status="active",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert data["expiring_deployments"] == []


async def test_failed_builds_respect_visibility(db_session, test_user, client_for):
    other = await _other_user(db_session)
    mine = await _project(db_session, test_user, "Mine")
    shared = await _project(db_session, other, "Shared with me")
    invisible = await _project(db_session, other, "Not mine")
    db_session.add(
        ProjectMember(project_id=shared.id, user_id=test_user.id, role="viewer", added_by=other.id)
    )
    for project in (mine, shared, invisible):
        db_session.add(
            CodegenBuild(
                project_id=project.id, status="failed", created_by=project.user_id,
                error={"code": "validation_failed", "message": "x"},
            )
        )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        names = {b["project_name"] for b in data["failed_builds"]}
        assert names == {"Mine", "Shared with me"}
        assert all(b["error_code"] == "validation_failed" for b in data["failed_builds"])


async def test_awaiting_resubmission_latest_wins(db_session, test_user, client_for):
    now = datetime.now(UTC)
    held = await _project(db_session, test_user, "Held")
    resubmitted = await _project(db_session, test_user, "Resubmitted")

    def _assessment(project_id, decision, created_at):
        return AiRiskAssessment(
            project_id=project_id, content_hash=uuid.uuid4().hex, rubric_version=1,
            score=45, level="medium", factors={}, status="scored",
            decision=decision, created_at=created_at,
            decided_at=created_at if decision != "pending" else None,
            notes="tighten data handling" if decision == "changes_requested" else None,
        )

    # latest decision on "Held" is changes_requested → appears
    db_session.add(_assessment(held.id, "changes_requested", now - timedelta(hours=3)))
    # "Resubmitted": changes_requested SUPERSEDED by a newer pending row → absent
    db_session.add(_assessment(resubmitted.id, "changes_requested", now - timedelta(hours=5)))
    db_session.add(_assessment(resubmitted.id, "pending", now - timedelta(hours=1)))
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert [a["project_name"] for a in data["awaiting_resubmission"]] == ["Held"]
        assert data["awaiting_resubmission"][0]["notes"] == "tighten data handling"


async def test_reviewer_sees_pending_count(db_session, test_user, client_for):
    other = await _other_user(db_session)
    project = await _project(db_session, other, "Reviewable")
    db_session.add(
        ReviewerGroupMember(group_name="managers", user_id=test_user.id, added_by=None)
    )
    db_session.add(
        AiRiskAssessment(
            project_id=project.id, content_hash=uuid.uuid4().hex, rubric_version=1,
            score=45, level="medium", factors={}, status="scored",
            decision="pending", assigned_group="managers", routed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert data["reviews"] == {"is_reviewer": True, "pending": 1}


async def test_admin_ops_line(db_session, test_user, client_for):
    db_session.add(Alert(kind="cost_platform", severity="warning", message="80% of cap"))
    db_session.add(Alert(kind="rate_limit", severity="critical", message="sustained 429s"))
    db_session.add(Alert(kind="cost_user", severity="info", message="fyi", status="acknowledged"))
    test_user.role = "admin"
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _fetch(client)
        assert data["ops"] == {"active_alerts": 2, "max_severity": "critical"}


# --------------- /users/me/deployments (the nav Deployments page, 17 Sep fix)


async def test_my_deployments_visibility_and_order(
    db_session, test_user, client_for, audit_db
):
    """Owned ∪ explicitly-shared visibility, newest first, name-joined;
    a stranger's deployment never appears (the projects-list clause)."""
    mine = await _project(db_session, test_user, name="Mine")
    other = await _other_user(db_session)
    shared = await _project(db_session, other, name="Shared With Me")
    db_session.add(ProjectMember(project_id=shared.id, user_id=test_user.id, role="viewer"))
    hidden = await _project(db_session, other, name="Not Mine")

    now = datetime.now(UTC)
    db_session.add(
        Deployment(
            project_id=mine.id, user_id=test_user.id, status="active",
            health="healthy", app_url="https://mine.example/prod",
            deployed_at=now - timedelta(hours=2),
            expires_at=now + timedelta(hours=20),
            created_at=now - timedelta(hours=2),
        )
    )
    db_session.add(
        Deployment(
            project_id=shared.id, user_id=other.id, status="torn_down",
            torn_down_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
        )
    )
    db_session.add(
        Deployment(
            project_id=hidden.id, user_id=other.id, status="active",
            created_at=now,
        )
    )
    await db_session.commit()

    async with client_for(test_user) as ac:
        r = await ac.get("/api/v1/users/me/deployments")
    assert r.status_code == 200, r.text
    rows = r.json()["deployments"]
    names = [row["project_name"] for row in rows]
    assert names == ["Shared With Me", "Mine"]  # newest first
    assert [row["mine"] for row in rows] == [False, True]
    assert "Not Mine" not in names  # stranger's deployment invisible
    active = rows[1]
    assert active["status"] == "active" and active["app_url"].startswith("https://mine")
    assert active["expires_at"] is not None


async def test_my_deployments_empty(test_user, client_for, audit_db):
    async with client_for(test_user) as ac:
        r = await ac.get("/api/v1/users/me/deployments")
    assert r.status_code == 200
    assert r.json() == {"deployments": []}
