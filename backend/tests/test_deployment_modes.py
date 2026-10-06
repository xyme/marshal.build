"""B20 deployment modes (R1): mode column, per-mode gate stance, policy keys.

Full Governance = today's enforcing gate, unchanged (regression-pinned here).
Testbed = same scoring pipeline, advisory (or off) stance — verdicts recorded,
never blocking; availability + stance + preselect are deployment policies.
"""

import uuid
from unittest.mock import patch

import pytest

from app.models import Deployment, PlatformSettings, Project
from app.services import deployment as deploy_service
from app.services import risk as risk_svc

pytestmark = pytest.mark.asyncio


def _swallow_spawn(deployment_id, coro):
    coro.close()


async def _project(db, user, name="Modes"):
    proj = Project(user_id=user.id, name=f"{name}-{uuid.uuid4().hex[:6]}")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


def _settings(**deployment_policies):
    return PlatformSettings(
        id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
        deployment_policies=deployment_policies,
    )


def _blocking_gate(monkeypatch):
    async def gate(db, project, user):
        raise risk_svc.GateBlocked("risk_pending", {"detail": "pending review"})

    monkeypatch.setattr(risk_svc, "deployment_gate", gate)


def _passing_gate(monkeypatch):
    async def gate(db, project, user):
        return None

    monkeypatch.setattr(risk_svc, "deployment_gate", gate)


# ------------------------------------------------- gate stance by mode


async def test_full_governance_still_blocks_on_pending(
    client, db_session, test_user, monkeypatch, audit_db
):
    """Regression pin: the default mode is today's behavior, exactly."""
    _blocking_gate(monkeypatch)
    project = await _project(db_session, test_user)
    resp = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "risk_pending"


async def test_testbed_advisory_deploys_through_pending(
    client, db_session, test_user, monkeypatch, audit_db
):
    """R1.3: advisory = the verdict is recorded, the deploy proceeds."""
    _blocking_gate(monkeypatch)
    project = await _project(db_session, test_user)
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/deploy", json={"mode": "testbed"}
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["mode"] == "testbed"


async def test_testbed_gate_off_skips_scoring(
    client, db_session, test_user, monkeypatch, audit_db
):
    db_session.add(_settings(testbed_gate_mode="off"))
    await db_session.commit()
    calls: list[int] = []

    async def gate(db, project, user):
        calls.append(1)

    monkeypatch.setattr(risk_svc, "deployment_gate", gate)
    project = await _project(db_session, test_user)
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/deploy", json={"mode": "testbed"}
        )
    assert resp.status_code == 202, resp.text
    assert calls == [], "off must skip scoring at deploy entirely"


# ------------------------------------------------- availability + immutability


async def test_testbed_disabled_by_policy_refuses(
    client, db_session, test_user, audit_db
):
    db_session.add(_settings(testbed_enabled=False))
    await db_session.commit()
    project = await _project(db_session, test_user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/deploy", json={"mode": "testbed"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "policy_testbed_disabled"


async def test_invalid_mode_is_422(client, db_session, test_user, audit_db):
    project = await _project(db_session, test_user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/deploy", json={"mode": "banana"}
    )
    assert resp.status_code == 422


async def test_update_inherits_mode_and_ignores_payload(
    client, db_session, test_user, monkeypatch, audit_db
):
    """R1.1: custody posture is immutable — updates inherit the active row's
    mode; a conflicting payload value changes nothing."""
    _passing_gate(monkeypatch)
    project = await _project(db_session, test_user)
    db_session.add(
        Deployment(
            project_id=project.id, user_id=test_user.id, status="active",
            mode="testbed", stack_name="marshal-x", stack_id="arn:x",
        )
    )
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/deploy", json={"mode": "full_governance"}
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["mode"] == "testbed"


async def test_update_of_testbed_rides_advisory_gate(
    client, db_session, test_user, monkeypatch, audit_db
):
    """An update of a testbed deployment keeps the testbed gate stance (a
    pending verdict must not brick in-place updates of a testbed)."""
    _blocking_gate(monkeypatch)
    project = await _project(db_session, test_user)
    db_session.add(
        Deployment(
            project_id=project.id, user_id=test_user.id, status="active",
            mode="testbed", stack_name="marshal-x", stack_id="arn:x",
        )
    )
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        resp = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert resp.status_code == 202, resp.text
    assert resp.json()["mode"] == "testbed"


async def test_default_mode_policy_preselects_testbed(
    client, db_session, test_user, monkeypatch, audit_db
):
    """R1.2: an event instance can make testbed the default; no payload
    needed. Gate stance follows the resolved mode."""
    db_session.add(_settings(default_mode="testbed"))
    await db_session.commit()
    _blocking_gate(monkeypatch)
    project = await _project(db_session, test_user)
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        resp = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert resp.status_code == 202, resp.text
    assert resp.json()["mode"] == "testbed"


# ------------------------------------------------- risk payload + policy keys


async def test_risk_payload_carries_mode_context(
    client, db_session, test_user, audit_db
):
    project = await _project(db_session, test_user)
    body = (await client.get(f"/api/v1/projects/{project.id}/risk")).json()
    assert body["gate_modes"] == {
        "full_governance": "enforce",
        "testbed": "advisory",
    }
    assert body["default_mode"] == "full_governance"


async def test_risk_payload_reports_disabled_testbed(
    client, db_session, test_user, audit_db
):
    db_session.add(_settings(testbed_enabled=False))
    await db_session.commit()
    project = await _project(db_session, test_user)
    body = (await client.get(f"/api/v1/projects/{project.id}/risk")).json()
    assert body["gate_modes"]["testbed"] == "disabled"


async def test_validate_policies_mode_keys():
    from app.services.policies import validate_policies

    for bad in (
        {"testbed_gate_mode": "enforce"},
        {"testbed_session_hours": 0},
        {"testbed_session_hours": 13},
        {"default_mode": "yolo"},
        {"testbed_enabled": "yes"},
    ):
        with pytest.raises(ValueError):
            validate_policies(bad)
    validate_policies(
        {
            "testbed_enabled": False,
            "testbed_gate_mode": "off",
            "testbed_session_hours": 8,
            "default_mode": "testbed",
        }
    )


# ------------------------------------------------- teardown reconciliation
# Live bug (27 Aug 2026, demo reseed): a failed in-place update leaves the
# prior deployment ACTIVE by design; tearing down the newer failed row then
# deleted the shared stack while the active row kept claiming "active", and
# the next deploy took the update path into a released account.


async def test_teardown_targets_the_running_deployment(
    client, db_session, test_user, monkeypatch, audit_db
):
    """The endpoint tears down what is RUNNING, not merely the newest row."""
    project = await _project(db_session, test_user)
    active = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        stack_name="marshal-shared", stack_id="arn:shared", timeline=[], resources=[],
    )
    failed_attempt = Deployment(
        project_id=project.id, user_id=test_user.id, status="failed",
        stack_name="marshal-shared", stack_id="arn:shared", timeline=[], resources=[],
        error="Update rolled back",
    )
    db_session.add_all([active, failed_attempt])
    await db_session.commit()
    active_id = active.id

    captured: list[uuid.UUID] = []

    async def fake_start_teardown(db, deployment):
        captured.append(deployment.id)

    monkeypatch.setattr(deploy_service, "start_teardown", fake_start_teardown)
    resp = await client.post(f"/api/v1/projects/{project.id}/deployment/teardown")
    assert resp.status_code == 202, resp.text
    assert captured == [active_id], "teardown must target the active deployment"


async def test_teardown_reconciles_siblings_sharing_the_stack(
    db_session, test_user, monkeypatch, audit_db
):
    """Once the stack and lease are gone, no sibling row may stay 'active'."""
    from app.models import Lease

    project = await _project(db_session, test_user)
    lease = Lease(
        provider="direct", external_lease_id="lease-share", aws_account_id="123456789012",
        status="active", project_id=project.id, user_id=test_user.id,
    )
    db_session.add(lease)
    await db_session.flush()
    stranded = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        stack_name="marshal-shared", lease_id=lease.id, timeline=[], resources=[],
    )
    tearing = Deployment(
        project_id=project.id, user_id=test_user.id, status="tearing_down",
        stack_name="marshal-shared", lease_id=lease.id, timeline=[], resources=[],
    )
    db_session.add_all([stranded, tearing])
    await db_session.commit()
    stranded_id, tearing_id = stranded.id, tearing.id

    class FakeProvider:
        name = "direct"

        def deployment_session(self, lease_info):
            class S:  # never exercised: stack_name is cleared below
                def client(self, kind):
                    raise AssertionError("no stack work expected")

            return S()

        async def terminate_lease(self, ref):
            return None

    monkeypatch.setattr(deploy_service, "get_sandbox_provider", lambda: FakeProvider())
    # stack_name is cleared so teardown skips CloudFormation entirely and we
    # exercise the lease-release + reconciliation tail deterministically.
    tearing.stack_name = None
    await db_session.commit()

    await deploy_service._run_teardown(tearing_id)

    db_session.expire_all()
    assert (await db_session.get(Deployment, tearing_id)).status == "torn_down"
    assert (await db_session.get(Deployment, stranded_id)).status == "torn_down", (
        "a sibling sharing the released lease cannot remain active"
    )
