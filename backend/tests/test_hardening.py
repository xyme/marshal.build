"""Pre-Beta security hardening (punch list, 6 Aug 2026): body-size backstop,
JIT email from Cognito, per-user deployment quota, export limiter."""

import uuid

import pytest

from app.models import Deployment, Project, User
from app.services import deployment as deploy_svc

pytestmark = pytest.mark.asyncio


# ------------------------------------------------- body-size backstop


async def test_declared_oversize_body_is_413(test_user, client_for):
    async with client_for(test_user) as client:
        r = await client.post(
            "/api/v1/projects",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(3 * 1024 * 1024),
            },
        )
        assert r.status_code == 413
        assert r.json()["detail"]["code"] == "payload_too_large"


async def test_streamed_oversize_body_is_413(test_user, client_for):
    async def chunks():
        for _ in range(3):  # 3 MiB in 1 MiB chunks, no content-length
            yield b"x" * (1024 * 1024)

    async with client_for(test_user) as client:
        r = await client.post(
            "/api/v1/projects",
            content=chunks(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413


async def test_normal_body_passes(test_user, client_for):
    async with client_for(test_user) as client:
        r = await client.post("/api/v1/projects", json={"name": "Small payload"})
        assert r.status_code in (200, 201)


# ------------------------------------------------- JIT email (Cognito, not header)


async def test_jit_email_ignores_header_uses_cognito(db_session, monkeypatch):
    from app.core import auth as auth_module

    sub = f"sub-{uuid.uuid4().hex[:10]}"

    class FakeClaims:
        pass

    claims = FakeClaims()
    claims.sub = sub
    claims.username = "jituser"
    claims.groups = ["power"]
    claims.role = "power"
    claims.amr = ["pwd"]
    claims.raw = {"sub": sub}
    claims.mfa_satisfied = False

    monkeypatch.setattr(auth_module, "validate_token", lambda t: claims)

    async def fake_email(access_token):
        assert access_token == "the-bearer-token"
        return "verified@marshal.demo"

    monkeypatch.setattr(auth_module, "_email_from_cognito", fake_email)

    class FakeRequest:
        headers = {
            "authorization": "Bearer the-bearer-token",
            "x-user-email": "attacker-controlled@evil.example",  # must be IGNORED
        }
        state = type("S", (), {})()

    user = await auth_module.get_current_user(FakeRequest(), db_session)
    assert user.email == "verified@marshal.demo"


async def test_jit_email_fallback_when_cognito_unavailable(db_session, monkeypatch):
    from app.core import auth as auth_module

    sub = f"sub-{uuid.uuid4().hex[:10]}"

    class FakeClaims:
        pass

    claims = FakeClaims()
    claims.sub = sub
    claims.username = "fallbackuser"
    claims.groups = ["power"]
    claims.role = "power"
    claims.amr = ["pwd"]
    claims.raw = {"sub": sub}
    claims.mfa_satisfied = False

    monkeypatch.setattr(auth_module, "validate_token", lambda t: claims)

    async def no_email(access_token):
        return None

    monkeypatch.setattr(auth_module, "_email_from_cognito", no_email)

    class FakeRequest:
        headers = {"authorization": "Bearer t"}
        state = type("S", (), {})()

    user = await auth_module.get_current_user(FakeRequest(), db_session)
    assert user.email == "fallbackuser@unknown"


# ------------------------------------------------- deployment quota


async def _project(db, owner: User, name: str) -> Project:
    project = Project(user_id=owner.id, name=name, status="spec_complete")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def test_deployment_quota_blocks_fourth_active(db_session, test_user):
    for i in range(3):
        p = await _project(db_session, test_user, f"Held {i}")
        db_session.add(
            Deployment(project_id=p.id, user_id=test_user.id, status="active")
        )
    await db_session.commit()

    target = await _project(db_session, test_user, "One too many")
    with pytest.raises(ValueError, match="per-user"):
        await deploy_svc.start_deployment(db_session, test_user, target)


async def test_deployment_quota_exempts_update_in_place(db_session, test_user, monkeypatch):
    """An active deployment on the SAME project routes to update — the quota
    must not block replacing a stack the user already holds."""
    projects = []
    for i in range(3):
        p = await _project(db_session, test_user, f"Held-u {i}")
        db_session.add(
            Deployment(project_id=p.id, user_id=test_user.id, status="active")
        )
        projects.append(p)
    await db_session.commit()

    # spawn is the async pipeline kickoff — neuter it; we assert row creation
    monkeypatch.setattr(deploy_svc, "_spawn_pipeline", lambda *a, **k: None, raising=False)
    try:
        deployment = await deploy_svc.start_deployment(
            db_session, test_user, projects[0]
        )
        assert deployment.status == "pending"
    except ValueError as exc:
        raise AssertionError(f"update-in-place was quota-blocked: {exc}") from exc


async def test_deployment_quota_admin_override_setting(db_session, test_user):
    from app.models import PlatformSettings

    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.rate_limits = {**(row.rate_limits or {}), "max_active_deployments_per_user": 1}
    await db_session.commit()

    p = await _project(db_session, test_user, "Quota one")
    db_session.add(Deployment(project_id=p.id, user_id=test_user.id, status="active"))
    await db_session.commit()

    target = await _project(db_session, test_user, "Blocked at one")
    with pytest.raises(ValueError, match="limit is 1"):
        await deploy_svc.start_deployment(db_session, test_user, target)


# ------------------------------------------------- export limiter


async def test_export_rate_limiter_429s(test_user, client_for, db_session):
    from app.services import exportlimit

    project = await _project(db_session, test_user, "Exportable")
    async with client_for(test_user) as client:
        exportlimit._windows.clear()
        # legitimate call first (may 404/422 on no specs — anything but 429)
        r = await client.get(f"/api/v1/projects/{project.id}/export")
        assert r.status_code != 429
        # saturate the window
        import time

        window = int(time.time() // 60)
        exportlimit._windows[str(test_user.id)] = (window, exportlimit.EXPORTS_PER_MINUTE)
        r = await client.get(f"/api/v1/projects/{project.id}/export")
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "export_rate_limited"
        assert r.headers.get("retry-after") == "60"
        exportlimit._windows.clear()


# ------------------------------------------------- B20 phase 0 hardening


async def test_quota_reads_deployment_policies_over_legacy(db_session, test_user):
    """B20 R0.5: deployment_policies is the quota's home — a value there wins
    over the legacy rate_limits location."""
    from app.models import PlatformSettings

    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.rate_limits = {**(row.rate_limits or {}), "max_active_deployments_per_user": 99}
    row.deployment_policies = {
        **(row.deployment_policies or {}),
        "max_active_deployments_per_user": 1,
    }
    await db_session.commit()

    p = await _project(db_session, test_user, "Policy quota")
    db_session.add(Deployment(project_id=p.id, user_id=test_user.id, status="active"))
    await db_session.commit()

    target = await _project(db_session, test_user, "Policy blocked")
    with pytest.raises(ValueError, match="limit is 1"):
        await deploy_svc.start_deployment(db_session, test_user, target)


async def test_resolver_falls_back_to_legacy_quota_location(db_session):
    """B20 R0.5: no deployment_policies key → the legacy rate_limits value is
    honored (existing customized settings keep working until an admin save)."""
    from app.models import PlatformSettings
    from app.services.policies import resolve_deployment_policies

    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.rate_limits = {"max_active_deployments_per_user": 7}
    row.deployment_policies = {}
    await db_session.commit()

    policies = await resolve_deployment_policies(db_session)
    assert policies.max_active_deployments_per_user == 7


async def test_provider_resolved_from_lease_row(monkeypatch):
    """B20 R0.3: operations on an existing lease use the provider RECORDED on
    it, not the boot-time global; nameless fakes fall back to the boot one."""
    import app.services.sandbox as sandbox_pkg

    class Boot:
        name = "direct"

    boot = Boot()
    sentinel = object()
    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: boot)
    monkeypatch.setattr(
        sandbox_pkg, "provider_by_name",
        lambda name: sentinel if name == "isb" else boot,
    )

    class IsbLease:
        provider = "isb"

    class DirectLease:
        provider = "direct"

    assert deploy_svc.provider_for_lease(IsbLease()) is sentinel
    assert deploy_svc.provider_for_lease(DirectLease()) is boot
    assert deploy_svc.provider_for_lease(None) is boot


async def test_lease_row_exists_before_activation_and_is_reaped(
    db_session, test_user, monkeypatch, audit_db
):
    """B20 R0.2: the Lease row carries the external id BEFORE the provider's
    activation wait; an activation failure terminates the provider lease and
    marks the row — no orphan on either side."""
    from app.models import Lease
    from app.services.sandbox import LeaseInfo

    terminated: list[str] = []

    class StallingProvider:
        name = "direct"

        async def request_lease(self, *, project_id, user_id, on_created=None):
            if on_created is not None:
                await on_created(
                    LeaseInfo(
                        external_lease_id="ext-b20",
                        aws_account_id=None,
                        status="requested",
                    )
                )
            raise RuntimeError("did not activate in time")

        async def terminate_lease(self, ref):
            terminated.append(ref)

    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: StallingProvider())

    project = await _project(db_session, test_user, "Orphanable")
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="pending",
        timeline=[], resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()
    dep_id = deployment.id

    await deploy_svc._run_deploy(dep_id)

    db_session.expire_all()
    refreshed = await db_session.get(Deployment, dep_id)
    assert refreshed.status == "failed"
    assert refreshed.lease_id is not None, "lease row must exist despite the crash"
    lease = await db_session.get(Lease, refreshed.lease_id)
    assert lease.external_lease_id == "ext-b20"
    assert lease.status == "terminated"
    assert terminated == ["ext-b20"]


# ------------------------------------------------- G26: lease released on pre-stack failure


def _g26_session(create_stack):
    """Fake deploy session: only the CloudFormation client is ever asked for."""

    class Cfn:
        pass

    cfn = Cfn()
    cfn.create_stack = create_stack

    class Session:
        def client(self, kind):
            assert kind == "cloudformation", kind
            return cfn

    return Session()


def _g26_template_error(**_kwargs):
    from botocore.exceptions import ClientError

    raise ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Template format error: Invalid outputs property : [Fn::Sub]",
            }
        },
        "CreateStack",
    )


async def _g26_deployment(db_session, test_user, name):
    project = await _project(db_session, test_user, name)
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="pending",
        timeline=[], resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()
    return deployment.id


async def test_failure_before_create_stack_releases_active_lease(
    db_session, test_user, monkeypatch, audit_db
):
    """G26: the direct provider's lease is `active` the moment it is requested;
    a create_stack rejection (the G25 template-format ValidationException) must
    release it — otherwise the row counts against the user's capacity until a
    DB edit, and teardown refuses it once a newer deployment row exists."""
    from app.models import Lease
    from app.services.policies import count_provider_reservations
    from app.services.sandbox import LeaseInfo

    terminated: list[str] = []

    class DirectLikeProvider:
        name = "direct"

        async def request_lease(self, *, project_id, user_id, on_created=None):
            return LeaseInfo(
                external_lease_id="direct-g26", aws_account_id="123456789012",
                status="active",
            )

        def deployment_session(self, lease_info):
            return _g26_session(_g26_template_error)

        async def terminate_lease(self, ref):
            terminated.append(ref)

    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: DirectLikeProvider())
    user_id = test_user.id
    dep_id = await _g26_deployment(db_session, test_user, "G26 released")

    await deploy_svc._run_deploy(dep_id)

    db_session.expire_all()
    refreshed = await db_session.get(Deployment, dep_id)
    assert refreshed.status == "failed"
    assert "Template format error" in (refreshed.error or "")
    assert refreshed.stack_id is None and refreshed.stack_name is None
    lease = await db_session.get(Lease, refreshed.lease_id)
    assert lease.status == "terminated"
    assert lease.terminated_at is not None
    assert terminated == ["direct-g26"]
    assert await count_provider_reservations(db_session, user_id) == 0, (
        "a released lease must stop counting against the user's capacity"
    )
    details = [entry.get("detail") for entry in refreshed.timeline]
    assert "lease released — deployment failed before stack creation" in details


async def test_failure_after_create_stack_leaves_lease_for_teardown(
    db_session, test_user, monkeypatch, audit_db
):
    """G26 boundary: once create_stack returned a StackId the stack exists, so
    a later failure leaves the lease exactly as before — reclaiming the account
    is the user's teardown, never the failure handler's."""
    from app.models import Lease
    from app.services.policies import count_provider_reservations
    from app.services.sandbox import LeaseInfo

    terminated: list[str] = []

    def create_stack(**_kwargs):
        return {"StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/marshal-g26/1"}

    class DirectLikeProvider:
        name = "direct"

        async def request_lease(self, *, project_id, user_id, on_created=None):
            return LeaseInfo(
                external_lease_id="direct-g26-kept", aws_account_id="123456789012",
                status="active",
            )

        def deployment_session(self, lease_info):
            return _g26_session(create_stack)

        async def terminate_lease(self, ref):
            terminated.append(ref)

    async def poll_blows_up(db, deployment, cfn, until_deleted):
        raise RuntimeError("lost the CloudFormation endpoint mid-poll")

    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: DirectLikeProvider())
    monkeypatch.setattr(deploy_svc, "_poll_stack", poll_blows_up)
    user_id = test_user.id
    dep_id = await _g26_deployment(db_session, test_user, "G26 kept")

    await deploy_svc._run_deploy(dep_id)

    db_session.expire_all()
    refreshed = await db_session.get(Deployment, dep_id)
    assert refreshed.status == "failed"
    assert refreshed.stack_id and refreshed.stack_name
    lease = await db_session.get(Lease, refreshed.lease_id)
    assert lease.status == "active"
    assert lease.terminated_at is None
    assert terminated == []
    assert await count_provider_reservations(db_session, user_id) == 1
    phases = [entry.get("phase") for entry in refreshed.timeline]
    assert "lease_released" not in phases


async def test_failure_before_create_stack_terminates_isb_lease_once(
    db_session, test_user, monkeypatch, audit_db
):
    """G26 on ISB: the release goes through the provider seam teardown uses —
    exactly one POST /leases/<id>/terminate — so the pooled account returns."""
    import httpx

    from app.models import Lease
    from app.services.sandbox import LeaseInfo
    from app.services.sandbox.isb import IsbHttpProvider

    hits: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "success", "data": {}})

    provider = IsbHttpProvider(
        base_url="https://isb.example.com/prod",
        transport=httpx.MockTransport(handler),
        jwt_secret="unit-test-secret-g26-lease-release-0123456789",
    )

    async def request_lease(*, project_id, user_id, on_created=None):
        info = LeaseInfo(
            external_lease_id="isb-lease-g26", aws_account_id="111122223333",
            status="active",
        )
        if on_created is not None:
            await on_created(info)
        return info

    monkeypatch.setattr(provider, "request_lease", request_lease)
    monkeypatch.setattr(
        provider, "deployment_session", lambda lease_info: _g26_session(_g26_template_error)
    )
    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: provider)
    dep_id = await _g26_deployment(db_session, test_user, "G26 isb")

    await deploy_svc._run_deploy(dep_id)

    db_session.expire_all()
    refreshed = await db_session.get(Deployment, dep_id)
    assert refreshed.status == "failed"
    lease = await db_session.get(Lease, refreshed.lease_id)
    assert lease.provider == "isb"
    assert lease.status == "terminated"
    assert lease.terminated_at is not None
    assert hits == [("POST", "/prod/leases/isb-lease-g26/terminate")]
