"""Project dashboard/lifecycle + admin user management tests
(project-admin-dashboard spec R1–R5)."""

import uuid

import pytest
from sqlalchemy import select

from app.models import AuditLog, Deployment, Project, Spec
from app.services import admin_users as admin_svc

pytestmark = pytest.mark.asyncio


async def _mk_project(db, user, name="Proj", status="draft", **overrides) -> Project:
    project = Project(user_id=user.id, name=name, status=status, **overrides)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


# ------------------------------------------------------------------- list view


async def test_list_filters_search_sort_counts(client, db_session, test_user, audit_db):
    p1 = await _mk_project(db_session, test_user, "HR FAQ Bot", "spec_complete")
    await _mk_project(db_session, test_user, "Invoice Parser", "draft")
    await _mk_project(db_session, test_user, "Old Thing", "archived")
    db_session.add(Spec(project_id=p1.id, version=1, type="requirements", content="x"))
    db_session.add(Spec(project_id=p1.id, version=2, type="requirements", content="y"))
    await db_session.commit()

    body = (await client.get("/api/v1/projects")).json()
    assert body["total"] == 2  # archived excluded by default
    assert body["archived_count"] == 1
    hr = next(i for i in body["items"] if i["name"] == "HR FAQ Bot")
    assert hr["spec_version_count"] == 2
    assert hr["last_activity_at"] is not None

    search = (await client.get("/api/v1/projects", params={"q": "faq"})).json()
    assert search["total"] == 1 and search["items"][0]["name"] == "HR FAQ Bot"

    archived = (await client.get("/api/v1/projects", params={"status": "archived"})).json()
    assert archived["total"] == 1 and archived["items"][0]["name"] == "Old Thing"

    by_name = (await client.get("/api/v1/projects", params={"sort": "name"})).json()
    assert [i["name"] for i in by_name["items"]] == ["HR FAQ Bot", "Invoice Parser"]


# ------------------------------------------------------- archive/restore/delete


async def test_archive_restore_round_trip(client, db_session, test_user, audit_db):
    project = await _mk_project(db_session, test_user, "Cycle", "spec_complete")
    db_session.add(Spec(project_id=project.id, version=1, type="requirements", content="keep me"))
    await db_session.commit()

    archived = (await client.post(f"/api/v1/projects/{project.id}/archive")).json()
    assert archived["status"] == "archived"

    restored = (await client.post(f"/api/v1/projects/{project.id}/restore")).json()
    assert restored["status"] == "spec_complete"  # recomputed from data
    spec = (
        await db_session.execute(select(Spec).where(Spec.project_id == project.id))
    ).scalar_one()
    assert spec.content == "keep me"  # AC-4: data intact


async def test_archive_blocked_while_building(client, db_session, test_user, audit_db):
    project = await _mk_project(db_session, test_user, "Busy", "building")
    resp = await client.post(f"/api/v1/projects/{project.id}/archive")
    assert resp.status_code == 409


async def test_restore_requires_archived(client, db_session, test_user, audit_db):
    project = await _mk_project(db_session, test_user, "NotArchived", "draft")
    assert (await client.post(f"/api/v1/projects/{project.id}/restore")).status_code == 409


async def test_delete_blocked_with_active_deployment(client, db_session, test_user, audit_db):
    project = await _mk_project(db_session, test_user, "Deployed", "deployed")
    deployment = Deployment(project_id=project.id, user_id=test_user.id, status="active")
    db_session.add(deployment)
    await db_session.commit()

    blocked = await client.delete(f"/api/v1/projects/{project.id}")
    assert blocked.status_code == 409
    assert "tear down" in blocked.json()["detail"].lower()

    deployment.status = "torn_down"
    await db_session.commit()
    assert (await client.delete(f"/api/v1/projects/{project.id}")).status_code == 204

    # Soft-deleted: hidden from list and detail
    assert (await client.get(f"/api/v1/projects/{project.id}")).status_code == 404
    body = (await client.get("/api/v1/projects")).json()
    assert body["total"] == 0
    await db_session.refresh(project)
    assert project.status == "deleted" and project.deleted_at is not None


# -------------------------------------------------------------------- activity


async def test_activity_timeline_and_filters(client, db_session, test_user, audit_db):
    created = (await client.post("/api/v1/projects", json={"name": "Traced"})).json()
    pid = created["id"]
    # Synthesize a deployment event row (deploy path needs live sandbox otherwise)
    db_session.add(
        AuditLog(
            actor_id=test_user.id, category="deployment", action="deploy_triggered",
            resource_type="project", resource_id=pid, project_id=uuid.UUID(pid), detail={},
        )
    )
    await db_session.commit()

    all_items = (await client.get(f"/api/v1/projects/{pid}/activity")).json()
    actions = [i["action"] for i in all_items["items"]]
    assert "project_created" in actions and "deploy_triggered" in actions

    deploys = (
        await client.get(f"/api/v1/projects/{pid}/activity", params={"filter": "deployments"})
    ).json()
    assert [i["action"] for i in deploys["items"]] == ["deploy_triggered"]

    system = (
        await client.get(f"/api/v1/projects/{pid}/activity", params={"filter": "system"})
    ).json()
    assert "project_created" in [i["action"] for i in system["items"]]
    assert "deploy_triggered" not in [i["action"] for i in system["items"]]


async def test_activity_not_visible_to_other_users(client_for, db_session, test_user, business_user, audit_db):
    project = await _mk_project(db_session, test_user, "Private")
    async with client_for(business_user) as ac:
        assert (await ac.get(f"/api/v1/projects/{project.id}/activity")).status_code == 404


# ------------------------------------------------------------------ admin users


class FakeCognito:
    def __init__(self, fail_on=None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_on = fail_on

    def _record(self, op, kwargs):
        self.calls.append((op, kwargs))
        if op == self.fail_on:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "InternalErrorException"}}, op)

    def admin_add_user_to_group(self, **kwargs):
        self._record("add", kwargs)

    def admin_remove_user_from_group(self, **kwargs):
        self._record("remove", kwargs)

    def admin_disable_user(self, **kwargs):
        self._record("disable", kwargs)

    def admin_enable_user(self, **kwargs):
        self._record("enable", kwargs)

    def admin_user_global_sign_out(self, **kwargs):
        self._record("global_sign_out", kwargs)


async def test_admin_users_list_and_stats(admin_user, client_for, db_session, test_user, audit_db):
    test_user.persona_upgrade_requested = True
    await db_session.commit()
    async with client_for(admin_user) as ac:
        body = (await ac.get("/api/v1/admin/users")).json()
        assert body["total"] == 2
        target = next(i for i in body["items"] if i["email"] == test_user.email)
        assert target["persona_upgrade_requested"] is True
        stats = (await ac.get("/api/v1/admin/users/stats")).json()
        assert stats["total"] == 2 and stats["admins"] == 1


async def test_admin_role_change_pins_and_mirrors(admin_user, client_for, db_session, test_user, monkeypatch, audit_db):
    fake = FakeCognito()
    monkeypatch.setattr(admin_svc, "_cognito", lambda: fake)
    async with client_for(admin_user) as ac:
        resp = await ac.put(f"/api/v1/admin/users/{test_user.id}", json={"role": "admin"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin" and resp.json()["role_source"] == "admin"
    ops = [op for op, _ in fake.calls]
    assert ops.count("remove") == 3 and ops.count("add") == 1
    add_kwargs = next(k for op, k in fake.calls if op == "add")
    assert add_kwargs["GroupName"] == "admin"
    await db_session.refresh(test_user)
    assert test_user.role == "admin" and test_user.role_source == "admin"


async def test_admin_role_change_rolls_back_on_cognito_failure(
    admin_user, client_for, db_session, test_user, monkeypatch, audit_db
):
    monkeypatch.setattr(admin_svc, "_cognito", lambda: FakeCognito(fail_on="add"))
    async with client_for(admin_user) as ac:
        resp = await ac.put(f"/api/v1/admin/users/{test_user.id}", json={"role": "admin"})
        assert resp.status_code == 502
    await db_session.refresh(test_user)
    assert test_user.role == "power" and test_user.role_source == "sso"  # unchanged


async def test_admin_suspend_and_self_guard(admin_user, client_for, db_session, test_user, monkeypatch, audit_db):
    fake = FakeCognito()
    monkeypatch.setattr(admin_svc, "_cognito", lambda: fake)
    async with client_for(admin_user) as ac:
        resp = await ac.put(f"/api/v1/admin/users/{test_user.id}", json={"status": "suspended"})
        assert resp.status_code == 200 and resp.json()["status"] == "suspended"
        assert ("disable", {"UserPoolId": admin_svc.get_settings().cognito_user_pool_id, "Username": test_user.cognito_sub}) in fake.calls
        # S15-03: suspension also revokes live sessions, not just new tokens
        assert ("global_sign_out", {"UserPoolId": admin_svc.get_settings().cognito_user_pool_id, "Username": test_user.cognito_sub}) in fake.calls

        self_demote = await ac.put(f"/api/v1/admin/users/{admin_user.id}", json={"role": "business"})
        assert self_demote.status_code == 422
        self_suspend = await ac.put(f"/api/v1/admin/users/{admin_user.id}", json={"status": "suspended"})
        assert self_suspend.status_code == 422


async def test_suspended_user_blocked_at_auth(db_session, test_user):
    """get_current_user 403s suspended accounts even with valid tokens (R4.2)."""
    from fastapi import HTTPException

    from app.core import auth as auth_module

    test_user.status = "suspended"
    await db_session.commit()

    class FakeRequest:
        headers = {"authorization": "Bearer t", "x-user-email": test_user.email}
        state = type("S", (), {})()

    class FakeClaims:
        sub = test_user.cognito_sub
        username = "u"
        groups = ["power"]
        role = "power"
        amr = ["pwd"]  # S14-02: no MFA marker
        raw = {"sub": test_user.cognito_sub}
        mfa_satisfied = False

    async def fake_validate(token):
        return FakeClaims()

    orig = auth_module.validate_token
    auth_module.validate_token = lambda t: FakeClaims()
    try:
        with pytest.raises(HTTPException) as exc:
            await auth_module.get_current_user(FakeRequest(), db_session)
        assert exc.value.status_code == 403
        assert "suspended" in exc.value.detail.lower()
    finally:
        auth_module.validate_token = orig


async def test_persona_request_approve_and_decline(admin_user, client_for, db_session, test_user, business_user, audit_db):
    business_user.persona_upgrade_requested = True
    test_user.persona = "business"
    test_user.persona_upgrade_requested = True
    await db_session.commit()
    async with client_for(admin_user) as ac:
        approved = await ac.post(
            f"/api/v1/admin/users/{test_user.id}/persona-request/decide", json={"approve": True}
        )
        assert approved.status_code == 200
        assert approved.json()["persona"] == "power"
        assert approved.json()["persona_upgrade_requested"] is False

        declined = await ac.post(
            f"/api/v1/admin/users/{business_user.id}/persona-request/decide", json={"approve": False}
        )
        assert declined.status_code == 200
        assert declined.json()["persona"] == "business"
        assert declined.json()["persona_upgrade_requested"] is False

        no_request = await ac.post(
            f"/api/v1/admin/users/{admin_user.id}/persona-request/decide", json={"approve": True}
        )
        assert no_request.status_code == 422


async def test_admin_users_role_gate(client, audit_db):
    assert (await client.get("/api/v1/admin/users")).status_code == 403
