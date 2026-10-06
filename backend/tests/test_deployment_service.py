"""Deployment guards + state machine rules (sandbox-deployment-poc spec R2/R3/R4)."""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import Deployment, Project
from app.services import deployment as deploy_service


def _swallow_spawn(deployment_id, coro):
    """Close the never-awaited pipeline coroutine to keep tests warning-free."""
    coro.close()


@pytest.fixture
async def project(db_session, test_user) -> Project:
    proj = Project(user_id=test_user.id, name="Test App")
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def test_start_deployment_creates_pending_and_spawns_task(
    db_session, test_user, project
):
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn) as spawn:
        deployment = await deploy_service.start_deployment(db_session, test_user, project)
    assert deployment.status == "pending"
    spawn.assert_called_once()


async def test_start_deployment_blocks_concurrent(db_session, test_user, project):
    db_session.add(
        Deployment(project_id=project.id, user_id=test_user.id, status="deploying")
    )
    await db_session.commit()
    with pytest.raises(ValueError, match="already in progress"):
        await deploy_service.start_deployment(db_session, test_user, project)


async def test_start_deployment_updates_in_place_when_active(db_session, test_user, project):
    """S11 R3: active deployments route to the update path — a NEW attempt row
    inheriting the running stack + lease; superseding happens on success."""
    active = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        stack_name="marshal-abc", stack_id="arn:stack/abc",
    )
    db_session.add(active)
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        attempt = await deploy_service.start_deployment(db_session, test_user, project)
    assert attempt.id != active.id
    assert attempt.status == "pending"
    assert attempt.stack_name == "marshal-abc"  # same stack (in-place)
    await db_session.refresh(active)
    assert active.status == "active"  # supersede only on UPDATE_COMPLETE


async def test_start_deployment_blocks_when_attempt_in_flight(db_session, test_user, project):
    db_session.add(Deployment(project_id=project.id, user_id=test_user.id, status="updating"))
    await db_session.commit()
    with pytest.raises(ValueError, match="already"):
        await deploy_service.start_deployment(db_session, test_user, project)


async def test_redeploy_allowed_after_torn_down(db_session, test_user, project):
    db_session.add(Deployment(project_id=project.id, user_id=test_user.id, status="torn_down"))
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn):
        deployment = await deploy_service.start_deployment(db_session, test_user, project)
    assert deployment.status == "pending"


async def test_teardown_only_from_active_or_failed(db_session, test_user, project):
    deployment = Deployment(project_id=project.id, user_id=test_user.id, status="deploying")
    db_session.add(deployment)
    await db_session.commit()
    with pytest.raises(ValueError, match="Cannot tear down"):
        await deploy_service.start_teardown(db_session, deployment)


async def test_teardown_transitions_to_tearing_down(db_session, test_user, project):
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active", timeline=[]
    )
    db_session.add(deployment)
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn) as spawn:
        await deploy_service.start_teardown(db_session, deployment)
    assert deployment.status == "tearing_down"
    assert deployment.timeline[-1]["phase"] == "tearing_down"
    spawn.assert_called_once()


async def test_stack_name_is_deterministic_and_short():
    pid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    name = deploy_service._stack_name(pid)
    assert name == "marshal-123456781234"
    assert len(name) <= 128


async def test_event_bus_pub_sub():
    bus = deploy_service.DeploymentEventBus()
    queue = await bus.subscribe("d1")
    await bus.publish("d1", {"type": "phase", "phase": "deploying"})
    event = queue.get_nowait()
    assert event["phase"] == "deploying"
    await bus.unsubscribe("d1", queue)
    await bus.publish("d1", {"type": "phase"})  # no subscribers — should not raise


# ------------------------------------------------- cloud admission by provider
# Install drill G14: `direct` in cloud admits on the policy concurrency bounds
# alone (no pool to exhaust, no provider uncertainty); `isb` keeps the
# fail-closed /accounts capacity check; the complete-snapshot requirement
# (admissions_paused, G13) applies to both.


class _CloudProvider:
    def __init__(self, name, snapshot=None, error=None):
        self.name = name
        self._snapshot = snapshot
        self._error = error
        self.snapshot_calls = 0

    async def capacity_snapshot(self):
        self.snapshot_calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


@pytest.fixture
def cloud_env(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "cloud")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def _store_complete_policies(db, **overrides):
    """A saved snapshot carrying every DEFAULTS key — what Admin → Model
    Controls → Save writes; without it cloud forces admissions_paused."""
    from app.models import PlatformSettings
    from app.services import policies as policies_svc

    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
    row.deployment_policies = {**policies_svc.DEFAULTS, **overrides}
    await db.commit()


async def test_cloud_admission_direct_admits_without_pool_check(
    cloud_env, db_session, test_user, project
):
    await _store_complete_policies(db_session)
    provider = _CloudProvider("direct", error=AssertionError("pool must not be read"))
    with (
        patch.object(deploy_service, "get_sandbox_provider", return_value=provider),
        patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn),
    ):
        deployment = await deploy_service.start_deployment(db_session, test_user, project)
    assert deployment.status == "pending"
    assert provider.snapshot_calls == 0


async def test_cloud_admission_isb_with_capacity_still_admits(
    cloud_env, db_session, test_user, project
):
    from app.services.sandbox import ProviderCapacity

    await _store_complete_policies(db_session, provider_capacity_buffer=1)
    provider = _CloudProvider(
        "isb", snapshot=ProviderCapacity(total_accounts=3, available_accounts=3, status_counts={})
    )
    with (
        patch.object(deploy_service, "get_sandbox_provider", return_value=provider),
        patch.object(deploy_service, "_spawn", side_effect=_swallow_spawn),
    ):
        deployment = await deploy_service.start_deployment(db_session, test_user, project)
    assert deployment.status == "pending"
    assert provider.snapshot_calls == 1


async def test_cloud_admission_isb_refuses_when_pool_cannot_keep_buffer(
    cloud_env, db_session, test_user, project
):
    from app.services.policies import PolicyViolation
    from app.services.sandbox import ProviderCapacity

    await _store_complete_policies(db_session, provider_capacity_buffer=1)
    provider = _CloudProvider(
        "isb", snapshot=ProviderCapacity(total_accounts=3, available_accounts=0, status_counts={})
    )
    with patch.object(deploy_service, "get_sandbox_provider", return_value=provider):
        with pytest.raises(PolicyViolation) as exc:
            await deploy_service.start_deployment(db_session, test_user, project)
    assert exc.value.code == "provider_capacity_buffer"
    rows = (await db_session.execute(select(Deployment))).scalars().all()
    assert rows == []  # refused under the lock: no pending row was published


@pytest.mark.parametrize(
    "provider",
    [
        _CloudProvider("isb", error=RuntimeError("ISB /accounts unreachable")),
        _CloudProvider("isb", snapshot=None),
    ],
    ids=["snapshot-raises", "snapshot-none"],
)
async def test_cloud_admission_isb_refuses_on_provider_uncertainty(
    cloud_env, db_session, test_user, project, provider
):
    from app.services.policies import PolicyViolation

    await _store_complete_policies(db_session)
    with patch.object(deploy_service, "get_sandbox_provider", return_value=provider):
        with pytest.raises(PolicyViolation) as exc:
            await deploy_service.start_deployment(db_session, test_user, project)
    assert exc.value.code == "provider_capacity_unavailable"


async def test_cloud_admission_unknown_provider_fails_closed(cloud_env, db_session):
    from app.services.policies import PolicyViolation, resolve_deployment_policies

    await _store_complete_policies(db_session)
    policies = await resolve_deployment_policies(db_session)
    provider = _CloudProvider("other")
    with patch.object(deploy_service, "get_sandbox_provider", return_value=provider):
        with pytest.raises(PolicyViolation) as exc:
            await deploy_service._enforce_provider_capacity(policies=policies, is_update=False)
    assert exc.value.code == "provider_capacity_unavailable"
    assert provider.snapshot_calls == 0


async def test_cloud_admission_direct_refuses_on_fresh_install_without_snapshot(
    cloud_env, db_session, test_user, project
):
    """G13: no saved snapshot → cloud forces admissions_paused, direct or not."""
    from app.services.policies import PolicyViolation

    provider = _CloudProvider("direct")
    with patch.object(deploy_service, "get_sandbox_provider", return_value=provider):
        with pytest.raises(PolicyViolation) as exc:
            await deploy_service.start_deployment(db_session, test_user, project)
    assert exc.value.code == "admissions_paused"


async def test_cloud_admission_direct_still_refuses_when_admissions_paused(
    cloud_env, db_session, test_user, project
):
    """The operator kill switch on a complete snapshot refuses direct too."""
    from app.services.policies import PolicyViolation

    await _store_complete_policies(db_session, admissions_paused=True)
    provider = _CloudProvider("direct")
    with patch.object(deploy_service, "get_sandbox_provider", return_value=provider):
        with pytest.raises(PolicyViolation) as exc:
            await deploy_service.start_deployment(db_session, test_user, project)
    assert exc.value.code == "admissions_paused"
