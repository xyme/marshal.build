"""S16-04 usage analytics: event writes, dedupe, funnel math, team rollups."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import Project, Team, TeamMember, UsageEvent, User
from app.services import analytics as svc

pytestmark = pytest.mark.asyncio


async def _user(db, email: str) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email=email,
        name=email.split("@")[0], role="power", persona="power",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_record_denormalizes_team_from_project(db_session, admin_user, audit_db):
    team = Team(name="Growth")
    db_session.add(team)
    await db_session.commit()
    project = Project(user_id=admin_user.id, name="P", status="draft", team_id=team.id)
    db_session.add(project)
    await db_session.commit()

    await svc.record(admin_user.id, event="build_started", project_id=project.id)

    row = (
        (await db_session.execute(select(UsageEvent).where(UsageEvent.event == "build_started")))
        .scalars().one()
    )
    assert row.team_id == team.id and row.project_id == project.id


async def test_sign_in_dedupes_per_day(db_session, admin_user, audit_db):
    today = datetime.now(UTC).date().isoformat()
    key = f"signin:{admin_user.id}:{today}"
    await svc.record(admin_user.id, event="sign_in", dedupe_key=key)
    await svc.record(admin_user.id, event="sign_in", dedupe_key=key)  # same day: swallowed

    count = len(
        (
            await db_session.execute(select(UsageEvent).where(UsageEvent.event == "sign_in"))
        ).scalars().all()
    )
    assert count == 1


async def test_funnel_counts_distinct_users_and_conversion(db_session, admin_user, audit_db):
    alice = await _user(db_session, "alice@marshal.demo")
    bob = await _user(db_session, "bob@marshal.demo")

    for user in (alice, bob):
        await svc.record(user.id, event="sign_in")
        await svc.record(user.id, event="chat_message")
        await svc.record(user.id, event="chat_message")  # volume ≠ users
    await svc.record(alice.id, event="spec_saved")
    await svc.record(alice.id, event="build_started")

    report = await svc.funnel(db_session, days=7)
    steps = {s["step"]: s for s in report["steps"]}

    assert steps["sign_in"]["users"] == 2
    assert steps["chat_message"]["users"] == 2
    assert steps["chat_message"]["events"] == 4
    assert steps["chat_message"]["conversion"] == 1.0
    assert steps["spec_saved"]["users"] == 1
    assert steps["spec_saved"]["conversion"] == 0.5
    assert steps["deploy_started"]["users"] == 0


async def test_team_rollups_split_personal_and_team(db_session, admin_user, audit_db):
    team = Team(name="Ops")
    db_session.add(team)
    await db_session.commit()
    db_session.add(TeamMember(team_id=team.id, user_id=admin_user.id, role="lead"))
    project = Project(user_id=admin_user.id, name="T", status="draft", team_id=team.id)
    personal = Project(user_id=admin_user.id, name="Mine", status="draft")
    db_session.add_all([project, personal])
    await db_session.commit()

    await svc.record(admin_user.id, event="build_started", project_id=project.id)
    await svc.record(admin_user.id, event="build_started", project_id=personal.id)

    rollups = await svc.team_rollups(db_session, days=7)
    by_name = {r["team"]: r for r in rollups}
    assert by_name["Ops"]["events"]["build_started"]["count"] == 1
    assert by_name["(personal)"]["events"]["build_started"]["count"] == 1


async def test_admin_analytics_endpoints(admin_user, client_for, db_session, audit_db):
    await svc.record(admin_user.id, event="sign_in")
    async with client_for(admin_user) as ac:
        summary = (await ac.get("/api/v1/admin/analytics/summary")).json()
        funnel = (await ac.get("/api/v1/admin/analytics/funnel?days=7")).json()
        dau = (await ac.get("/api/v1/admin/analytics/daily-active?days=7")).json()

    assert summary["active_users"] >= 1 and summary["registered_users"] >= 1
    assert [s["step"] for s in funnel["steps"]] == svc.FUNNEL_STEPS
    assert dau[-1]["users"] >= 1


async def test_analytics_admin_only(test_user, client_for, audit_db):
    async with client_for(test_user) as ac:
        resp = await ac.get("/api/v1/admin/analytics/summary")
    assert resp.status_code == 403


async def test_me_bootstrap_tracks_sign_in(test_user, client_for, db_session, audit_db):
    """The /users/me call is funnel step 1 — verify the seam actually fires."""
    import asyncio

    async with client_for(test_user) as ac:
        assert (await ac.get("/api/v1/users/me")).status_code == 200
        await asyncio.sleep(0.05)  # let the fire-and-forget task land

    rows = (
        await db_session.execute(select(UsageEvent).where(UsageEvent.event == "sign_in"))
    ).scalars().all()
    assert len(rows) == 1 and rows[0].user_id == test_user.id
