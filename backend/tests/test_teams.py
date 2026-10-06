"""S15-02 team workspaces: access resolution, isolation, assignment rules."""

import uuid

import pytest
from sqlalchemy import select

from app.models import Project, ProjectMember, Team, TeamMember, User
from app.services import collab
from app.services import teams as teams_svc

pytestmark = pytest.mark.asyncio


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


async def _team(db, name: str, actor: User) -> Team:
    return await teams_svc.create_team(db, name=name, description=None, actor=actor)


async def _project(db, owner: User, name: str, team: Team | None = None) -> Project:
    project = Project(
        user_id=owner.id, name=name, status="draft",
        team_id=team.id if team else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


# ------------------------------------------------------------ access resolution


async def test_team_member_reads_team_project(db_session, test_user):
    lead = test_user
    reader = await _user(db_session, "reader@marshal.demo")
    team = await _team(db_session, "Retail Banking", lead)
    await teams_svc.set_members(
        db_session, team,
        [{"user_id": lead.id, "role": "lead"}, {"user_id": reader.id, "role": "member"}],
        lead,
    )
    project = await _project(db_session, lead, "Team project", team)

    resolved = await collab.resolve_role(db_session, project.id, reader)
    assert resolved is not None and resolved[1] == "viewer"
    # Leads edit
    lead_resolved = await collab.resolve_role(db_session, project.id, lead)
    assert lead_resolved[1] == "owner"  # also the owner here


async def test_team_lead_gets_editor_on_someone_elses_team_project(db_session, test_user):
    owner = test_user
    lead = await _user(db_session, "lead@marshal.demo")
    team = await _team(db_session, "Risk", owner)
    await teams_svc.set_members(
        db_session, team,
        [{"user_id": owner.id, "role": "member"}, {"user_id": lead.id, "role": "lead"}],
        owner,
    )
    project = await _project(db_session, owner, "Risk model", team)

    resolved = await collab.resolve_role(db_session, project.id, lead)
    assert resolved[1] == "editor"


async def test_non_member_cannot_see_team_project(db_session, test_user):
    """The isolation invariant: another team's work is invisible, not merely
    read-only."""
    owner = test_user
    outsider = await _user(db_session, "outsider@marshal.demo")
    team = await _team(db_session, "Payments", owner)
    await teams_svc.set_members(db_session, team, [{"user_id": owner.id, "role": "lead"}], owner)
    project = await _project(db_session, owner, "Payments rails", team)

    assert await collab.resolve_role(db_session, project.id, outsider) is None


async def test_personal_projects_unaffected_by_teams(db_session, test_user):
    owner = test_user
    other = await _user(db_session, "other@marshal.demo")
    team = await _team(db_session, "Ops", owner)
    await teams_svc.set_members(
        db_session, team,
        [{"user_id": owner.id, "role": "lead"}, {"user_id": other.id, "role": "member"}],
        owner,
    )
    personal = await _project(db_session, owner, "Personal", None)  # team_id NULL

    # Team membership grants nothing on a project that is not filed to the team
    assert await collab.resolve_role(db_session, personal.id, other) is None


async def test_explicit_project_grant_is_never_lowered_by_team_role(db_session, test_user):
    owner = test_user
    collaborator = await _user(db_session, "collab@marshal.demo")
    team = await _team(db_session, "Treasury", owner)
    await teams_svc.set_members(
        db_session, team,
        [{"user_id": owner.id, "role": "lead"}, {"user_id": collaborator.id, "role": "member"}],
        owner,
    )
    project = await _project(db_session, owner, "Treasury tool", team)
    # Explicit editor grant + team member (viewer) → editor must win
    db_session.add(
        ProjectMember(project_id=project.id, user_id=collaborator.id, role="editor")
    )
    await db_session.commit()

    resolved = await collab.resolve_role(db_session, project.id, collaborator)
    assert resolved[1] == "editor"


# ------------------------------------------------------------------ membership


async def test_set_members_is_declarative(db_session, test_user):
    lead = test_user
    a = await _user(db_session, "a@marshal.demo")
    b = await _user(db_session, "b@marshal.demo")
    team = await _team(db_session, "Declarative", lead)

    await teams_svc.set_members(
        db_session, team,
        [{"user_id": a.id, "role": "member"}, {"user_id": b.id, "role": "member"}], lead,
    )
    assert len(await teams_svc.members_with_users(db_session, team.id)) == 2

    # Sending only `a` (promoted) removes `b`
    members = await teams_svc.set_members(
        db_session, team, [{"user_id": a.id, "role": "lead"}], lead
    )
    assert [(m["email"], m["team_role"]) for m in members] == [("a@marshal.demo", "lead")]


async def test_duplicate_team_name_rejected(db_session, test_user):
    await _team(db_session, "Unique", test_user)
    with pytest.raises(teams_svc.TeamError):
        await _team(db_session, "Unique", test_user)


async def test_invalid_team_role_rejected(db_session, test_user):
    team = await _team(db_session, "Roles", test_user)
    with pytest.raises(teams_svc.TeamError):
        await teams_svc.set_members(
            db_session, team, [{"user_id": test_user.id, "role": "owner"}], test_user
        )


async def test_remove_user_everywhere_clears_memberships(db_session, test_user):
    leaver = await _user(db_session, "leaver@marshal.demo")
    for name in ("T1", "T2"):
        team = await _team(db_session, name, test_user)
        await teams_svc.set_members(
            db_session, team, [{"user_id": leaver.id, "role": "member"}], test_user
        )

    removed = await teams_svc.remove_user_everywhere(db_session, leaver.id)
    assert removed == 2
    remaining = (
        await db_session.execute(select(TeamMember).where(TeamMember.user_id == leaver.id))
    ).scalars().all()
    assert remaining == []


# ------------------------------------------------------------------ assignment


async def test_owner_can_only_assign_to_own_team(db_session, test_user):
    owner = test_user
    admin = await _user(db_session, "admin2@marshal.demo", role="admin")
    joined = await _team(db_session, "Joined", owner)
    foreign = await _team(db_session, "Foreign", admin)
    await teams_svc.set_members(db_session, joined, [{"user_id": owner.id, "role": "lead"}], owner)
    project = await _project(db_session, owner, "Assignable", None)

    with pytest.raises(teams_svc.TeamError):
        await teams_svc.assign_project(db_session, project, foreign.id, owner)

    assigned = await teams_svc.assign_project(db_session, project, joined.id, owner)
    assert assigned.team_id == joined.id

    # Admins may file any project anywhere (they administer the org)
    moved = await teams_svc.assign_project(db_session, project, foreign.id, admin)
    assert moved.team_id == foreign.id

    # ...and back to personal
    personal = await teams_svc.assign_project(db_session, project, None, admin)
    assert personal.team_id is None


async def test_non_owner_cannot_reassign(db_session, test_user):
    owner = test_user
    stranger = await _user(db_session, "stranger@marshal.demo")
    team = await _team(db_session, "Guarded", owner)
    project = await _project(db_session, owner, "Guarded project", None)
    with pytest.raises(teams_svc.TeamError):
        await teams_svc.assign_project(db_session, project, team.id, stranger)


# ------------------------------------------------------------------ API surface


async def test_projects_list_includes_team_projects(client, db_session, test_user, audit_db):
    """The list seam must widen to team projects (and label them)."""
    teammate_owner = await _user(db_session, "owner2@marshal.demo")
    team = await _team(db_session, "Shared", test_user)
    await teams_svc.set_members(
        db_session, team,
        [{"user_id": test_user.id, "role": "member"},
         {"user_id": teammate_owner.id, "role": "lead"}],
        test_user,
    )
    await _project(db_session, teammate_owner, "Teammate project", team)
    await _project(db_session, teammate_owner, "Private project", None)

    data = (await client.get("/api/v1/projects")).json()
    names = {p["name"]: p for p in data["items"]}
    assert "Teammate project" in names
    assert names["Teammate project"]["team_name"] == "Shared"
    assert names["Teammate project"]["my_role"] == "viewer"
    assert "Private project" not in names


async def test_admin_team_endpoints(admin_user, client_for, db_session, audit_db):
    member = await _user(db_session, "member@marshal.demo")
    async with client_for(admin_user) as ac:
        created = await ac.post(
            "/api/v1/admin/teams", json={"name": "Wholesale", "description": "WB tech"}
        )
        assert created.status_code == 201
        team_id = created.json()["id"]

        dupe = await ac.post("/api/v1/admin/teams", json={"name": "Wholesale"})
        assert dupe.status_code == 422

        set_members = await ac.put(
            f"/api/v1/admin/teams/{team_id}/members",
            json={"members": [{"user_id": str(member.id), "role": "lead"}]},
        )
        assert set_members.status_code == 200
        assert set_members.json()["members"][0]["team_role"] == "lead"

        renamed = await ac.put(
            f"/api/v1/admin/teams/{team_id}", json={"name": "Wholesale Banking"}
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "Wholesale Banking"

        archived = await ac.put(f"/api/v1/admin/teams/{team_id}", json={"status": "archived"})
        assert archived.json()["status"] == "archived"
        # Archived teams drop out of the default list but remain retrievable
        assert (await ac.get("/api/v1/admin/teams")).json() == []
        assert len((await ac.get("/api/v1/admin/teams?include_archived=true")).json()) == 1


async def test_team_roster_hidden_from_non_members(client, db_session, test_user, audit_db):
    other = await _user(db_session, "outsider2@marshal.demo")
    team = await _team(db_session, "Closed", other)
    await teams_svc.set_members(db_session, team, [{"user_id": other.id, "role": "lead"}], other)

    # test_user is not a member → 404, existence not disclosed
    assert (await client.get(f"/api/v1/teams/{team.id}/members")).status_code == 404
    assert (await client.get("/api/v1/teams")).json() == []


async def test_project_team_endpoint_round_trip(client, db_session, test_user, audit_db):
    team = await _team(db_session, "Endpoint", test_user)
    await teams_svc.set_members(
        db_session, team, [{"user_id": test_user.id, "role": "lead"}], test_user
    )
    project = await _project(db_session, test_user, "Filed", None)

    assigned = await client.put(
        f"/api/v1/projects/{project.id}/team", json={"team_id": str(team.id)}
    )
    assert assigned.status_code == 200 and assigned.json()["team_id"] == str(team.id)

    detail = (await client.get(f"/api/v1/projects/{project.id}")).json()
    assert detail["team_name"] == "Endpoint"

    cleared = await client.put(f"/api/v1/projects/{project.id}/team", json={"team_id": None})
    assert cleared.json()["team_id"] is None


# ----------------------------------------------- per-team budgets (5 Sep 2026)


async def test_admin_sets_and_clears_team_budget(admin_user, client_for, db_session, audit_db):
    async with client_for(admin_user) as ac:
        created = await ac.post("/api/v1/admin/teams", json={"name": "Budgeted"})
        team_id = created.json()["id"]
        assert created.json()["budget_usd"] is None

        set_budget = await ac.put(
            f"/api/v1/admin/teams/{team_id}", json={"budget_usd": 40}
        )
        assert set_budget.status_code == 200
        assert set_budget.json()["budget_usd"] == 40.0

        listed = await ac.get("/api/v1/admin/teams")
        assert listed.json()[0]["budget_usd"] == 40.0

        cleared = await ac.put(
            f"/api/v1/admin/teams/{team_id}", json={"clear_budget": True}
        )
        assert cleared.json()["budget_usd"] is None

        negative = await ac.put(
            f"/api/v1/admin/teams/{team_id}", json={"budget_usd": -5}
        )
        assert negative.status_code == 422
