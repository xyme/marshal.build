"""S16-03 chargeback: allocation by user/project/team, CE fold, CSV, rollups."""

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from app.models import Lease, ModelInvocation, Project, SandboxSpend, Team, User
from app.services import costs as svc

pytestmark = pytest.mark.asyncio

PERIOD = datetime.now(UTC).strftime("%Y-%m")


def _invocation(user_id, project_id, usd: float) -> ModelInvocation:
    return ModelInvocation(
        user_id=user_id,
        project_id=project_id,
        purpose="chat",
        model_id="us.anthropic.claude-sonnet-5",
        prompt_text="p",
        prompt_sha256=hashlib.sha256(b"p").hexdigest(),
        input_tokens=10,
        output_tokens=10,
        cost_usd=usd,
    )


async def _seed(db, admin_user):
    team = Team(name="FinServ")
    db.add(team)
    await db.commit()
    alice = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="alice@marshal.demo",
        name="alice", role="power", persona="power",
    )
    db.add(alice)
    await db.commit()
    team_project = Project(user_id=alice.id, name="Ledger", status="draft", team_id=team.id)
    personal = Project(user_id=admin_user.id, name="Solo", status="draft")
    db.add_all([team_project, personal])
    await db.commit()
    db.add_all(
        [
            _invocation(alice.id, team_project.id, 1.25),
            _invocation(alice.id, team_project.id, 0.75),
            _invocation(admin_user.id, personal.id, 0.50),
        ]
    )
    await db.commit()
    return alice, team_project, personal


async def test_chargeback_allocates_by_user_project_team(db_session, admin_user, audit_db):
    alice, team_project, _personal = await _seed(db_session, admin_user)

    report = await svc.chargeback(db_session, PERIOD)

    assert report["cost_explorer_enabled"] is False
    top = report["items"][0]
    assert top["email"] == "alice@marshal.demo"
    assert top["project"] == "Ledger" and top["team"] == "FinServ"
    assert top["model_usd"] == 2.0 and top["enclave_usd"] is None

    by_team = {t["team"]: t for t in report["by_team"]}
    assert by_team["FinServ"]["total_usd"] == 2.0
    assert by_team["(personal)"]["total_usd"] == 0.5

    assert len(report["monthly"]) == 1  # default months=1
    assert report["monthly"][0]["period"] == PERIOD
    assert report["monthly"][0]["model_usd"] == 2.5


async def test_chargeback_folds_enclave_spend_to_project_owner(
    db_session, admin_user, audit_db, monkeypatch
):
    alice, team_project, _ = await _seed(db_session, admin_user)

    class S:
        cost_explorer_enabled = True

    monkeypatch.setattr("app.core.config.get_settings", lambda: S())
    lease = Lease(provider="direct", project_id=team_project.id, user_id=alice.id)
    db_session.add(lease)
    await db_session.commit()
    db_session.add(
        SandboxSpend(
            lease_id=lease.id, project_id=team_project.id,
            date=datetime.now(UTC).replace(day=2), usd=3.40,
        )
    )
    await db_session.commit()

    report = await svc.chargeback(db_session, PERIOD)

    assert report["cost_explorer_enabled"] is True
    ledger = next(i for i in report["items"] if i["project"] == "Ledger")
    assert ledger["enclave_usd"] == 3.40
    assert ledger["total_usd"] == 5.40  # 2.00 model + 3.40 infra
    assert report["monthly"][0]["enclave_usd"] == 3.40


async def test_chargeback_csv_and_endpoint(admin_user, client_for, db_session, audit_db):
    await _seed(db_session, admin_user)

    async with client_for(admin_user) as ac:
        js = (await ac.get(f"/api/v1/admin/costs/chargeback?period={PERIOD}&months=3")).json()
        csv_resp = await ac.get(f"/api/v1/admin/costs/chargeback?period={PERIOD}&format=csv")

    assert len(js["monthly"]) == 3
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    lines = csv_resp.text.strip().split("\n")
    assert lines[0].startswith("period,email,project,team,calls")
    assert any("alice@marshal.demo" in line and "FinServ" in line for line in lines[1:])


# ------------------------------------------ per-team budget vs spend (5 Sep)


async def test_team_costs_joins_budgets_and_spend(
    db_session, admin_user, client_for, audit_db
):
    alice, team_project, _personal = await _seed(db_session, admin_user)
    # Budget the seeded team; add a second budgeted team with zero spend.
    from sqlalchemy import select as sa_select

    team = (
        (await db_session.execute(sa_select(Team).where(Team.name == "FinServ")))
        .scalars().one()
    )
    team.budget_usd = 10
    db_session.add(Team(name="Idle Team", budget_usd=25))
    await db_session.commit()

    async with client_for(admin_user) as ac:
        resp = await ac.get("/api/v1/admin/costs/teams")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows = {r["team"]: r for r in body["items"]}

        # Budgeted team with spend: pct computed from the chargeback total
        finserv = rows["FinServ"]
        assert finserv["budget_usd"] == 10.0
        assert finserv["total_usd"] > 0
        assert finserv["pct_used"] == round(finserv["total_usd"] / 10.0 * 100, 1)

        # Budgeted team with no spend still appears
        idle = rows["Idle Team"]
        assert idle["total_usd"] == 0.0 and idle["pct_used"] == 0.0

        # Unattributed spend surfaces as the (personal) bucket, unbudgeted
        personal = rows["(personal)"]
        assert personal["budget_usd"] is None and personal["total_usd"] > 0

        # Consumed budgets sort before unbudgeted rows
        assert body["items"][0]["team"] == "FinServ"


async def test_breakdown_by_team_pivot(db_session, admin_user, client_for, audit_db):
    await _seed(db_session, admin_user)
    async with client_for(admin_user) as ac:
        resp = await ac.get("/api/v1/admin/costs/breakdown?group_by=team")
        assert resp.status_code == 200, resp.text
        rows = {r["team"]: r for r in resp.json()["items"]}
        # Seed: team project (FinServ) + personal project both carry spend
        assert rows["FinServ"]["usd"] > 0 and rows["FinServ"]["calls"] > 0
        assert rows["(personal)"]["usd"] > 0
        # invalid pivot still refused
        assert (
            await ac.get("/api/v1/admin/costs/breakdown?group_by=nope")
        ).status_code == 422
