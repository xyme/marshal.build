"""S15-03 user lifecycle: offboarding propagation and access review."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Project, ProjectMember, TeamMember, User
from app.services import admin_users as svc
from app.services import teams as teams_svc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def cognito_calls(monkeypatch):
    """Offboarding suspends through the Cognito mirror; stub the client so the
    test exercises OUR propagation, not AWS. Yields the recorded (op, username)
    calls so tests can assert session revocation actually happened (S15-03)."""

    class _FakeCognito:
        calls: list[tuple[str, str]] = []  # (operation, username) — shared per test

        def admin_disable_user(self, **kwargs):
            self.calls.append(("disable", kwargs.get("Username", "")))
            return {}

        def admin_enable_user(self, **kwargs):
            self.calls.append(("enable", kwargs.get("Username", "")))
            return {}

        def admin_add_user_to_group(self, **_kwargs):
            return {}

        def admin_remove_user_from_group(self, **_kwargs):
            return {}

        def admin_user_global_sign_out(self, **kwargs):
            self.calls.append(("global_sign_out", kwargs.get("Username", "")))
            return {}

    _FakeCognito.calls = []
    monkeypatch.setattr(svc, "_cognito", lambda: _FakeCognito())
    return _FakeCognito.calls


async def _user(db, email: str, role: str = "power") -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email=email,
        name=email.split("@")[0],
        role=role,
        persona="power",
        onboarding_completed=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_offboarding_strips_all_shared_access(db_session, admin_user):
    leaver = await _user(db_session, "leaver@marshal.demo")
    owner = await _user(db_session, "owner@marshal.demo")

    team = await teams_svc.create_team(db_session, name="Ops", description=None, actor=admin_user)
    await teams_svc.set_members(
        db_session, team, [{"user_id": leaver.id, "role": "lead"}], admin_user
    )

    shared = Project(user_id=owner.id, name="Shared", status="draft")
    owned = Project(user_id=leaver.id, name="Their own work", status="draft")
    db_session.add_all([shared, owned])
    await db_session.commit()
    db_session.add(ProjectMember(project_id=shared.id, user_id=leaver.id, role="editor"))
    await db_session.commit()

    result = await svc.offboard_user(db_session, leaver, admin_user)

    assert result["status"] == "suspended"
    assert result["teams_removed"] == 1
    assert result["project_shares_removed"] == 1
    # Owned work is REPORTED for transfer, never silently deleted or moved
    assert [p["name"] for p in result["owned_projects_needing_transfer"]] == ["Their own work"]
    assert (
        await db_session.execute(select(TeamMember).where(TeamMember.user_id == leaver.id))
    ).first() is None
    assert (
        await db_session.execute(select(ProjectMember).where(ProjectMember.user_id == leaver.id))
    ).first() is None
    still_there = await db_session.get(Project, owned.id)
    assert still_there is not None and still_there.user_id == leaver.id


async def test_suspension_alone_preserves_memberships(db_session, admin_user):
    """Suspension is reversible — it must NOT destroy membership, or re-enabling
    someone back from leave would silently lose their access."""
    user = await _user(db_session, "onleave@marshal.demo")
    team = await teams_svc.create_team(db_session, name="Leave", description=None, actor=admin_user)
    await teams_svc.set_members(
        db_session, team, [{"user_id": user.id, "role": "member"}], admin_user
    )

    await svc.update_user(db_session, user, admin_user, status="suspended")

    memberships = (
        await db_session.execute(select(TeamMember).where(TeamMember.user_id == user.id))
    ).scalars().all()
    assert len(memberships) == 1


async def test_suspension_revokes_sessions(db_session, admin_user, cognito_calls):
    """S15-03: disable blocks new tokens; global sign-out kills refresh tokens.
    Both must fire, in that order, or a suspended user keeps a live session."""
    user = await _user(db_session, "revoked@marshal.demo")

    await svc.update_user(db_session, user, admin_user, status="suspended")

    ops = [op for op, username in cognito_calls if username == user.cognito_sub]
    assert ops == ["disable", "global_sign_out"]


async def test_offboard_already_suspended_still_signs_out(db_session, admin_user, cognito_calls):
    """A user suspended long ago (possibly before suspend revoked sessions)
    must still lose any surviving session when offboarded."""
    user = await _user(db_session, "oldsuspend@marshal.demo")
    user.status = "suspended"
    await db_session.commit()

    result = await svc.offboard_user(db_session, user, admin_user)

    assert result["status"] == "suspended"
    assert ("global_sign_out", user.cognito_sub) in cognito_calls
    # No disable: the account was already suspended — the sign-out is the point
    assert ("disable", user.cognito_sub) not in cognito_calls


async def test_cannot_offboard_yourself(db_session, admin_user):
    with pytest.raises(svc.AdminUserError):
        await svc.offboard_user(db_session, admin_user, admin_user)


async def test_offboard_endpoint(admin_user, client_for, db_session, audit_db):
    leaver = await _user(db_session, "bye@marshal.demo")
    async with client_for(admin_user) as ac:
        resp = await ac.post(f"/api/v1/admin/users/{leaver.id}/offboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "suspended" and body["email"] == "bye@marshal.demo"


# ------------------------------------------------------------- access review


async def test_access_review_flags_dormant_and_lists_teams(db_session, admin_user):
    active = await _user(db_session, "active@marshal.demo")
    dormant = await _user(db_session, "dormant@marshal.demo")
    suspended = await _user(db_session, "gone@marshal.demo")
    suspended.status = "suspended"
    await db_session.commit()

    team = await teams_svc.create_team(db_session, name="Risk", description=None, actor=admin_user)
    await teams_svc.set_members(
        db_session, team, [{"user_id": active.id, "role": "lead"}], admin_user
    )

    # Activity is derived from the audit trail: recent for one, stale for the other
    db_session.add(
        AuditLog(actor_id=active.id, category="user", action="login",
                 created_at=datetime.now(UTC) - timedelta(days=1))
    )
    db_session.add(
        AuditLog(actor_id=dormant.id, category="user", action="login",
                 created_at=datetime.now(UTC) - timedelta(days=200))
    )
    await db_session.commit()

    report = await svc.access_review(db_session, dormant_days=90)
    by_email = {row["email"]: row for row in report["users"]}

    assert by_email["active@marshal.demo"]["dormant"] is False
    assert by_email["active@marshal.demo"]["teams"] == ["Risk:lead"]
    assert by_email["dormant@marshal.demo"]["dormant"] is True
    # Suspended users are not "dormant" — they are already off
    assert by_email["gone@marshal.demo"]["dormant"] is False
    assert report["dormant_count"] >= 1
    assert report["admin_count"] >= 1


async def test_access_review_never_seen_counts_as_dormant(db_session, admin_user):
    await _user(db_session, "neverloggedin@marshal.demo")
    report = await svc.access_review(db_session, dormant_days=30)
    row = next(r for r in report["users"] if r["email"] == "neverloggedin@marshal.demo")
    assert row["last_active_at"] is None and row["dormant"] is True


async def test_access_review_csv_export(admin_user, client_for, db_session, audit_db):
    await _user(db_session, "reviewme@marshal.demo")
    async with client_for(admin_user) as ac:
        js = await ac.get("/api/v1/admin/users/access-review")
        csv_resp = await ac.get("/api/v1/admin/users/access-review?format=csv")

    assert js.status_code == 200 and js.json()["user_count"] >= 2
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "reviewme@marshal.demo" in csv_resp.text
    assert "platform_role" in csv_resp.text.split("\n")[0]
