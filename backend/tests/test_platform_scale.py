"""S12 platform-scale-policies: policy matrix, shared-state contention,
event-relay semantics, scheduler election, tenant isolation."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models import Deployment, PlatformSettings, Project
from app.models.entities import PLATFORM_TENANT_ID
from app.services import policies as policies_svc
from app.services.policies import (
    DEFAULTS,
    PolicyViolation,
    enforce_deploy_preflight,
    resolve_deployment_policies,
    validate_policies,
)

pytestmark = pytest.mark.asyncio

TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


# ------------------------------------------------------------- policy matrix


async def test_policies_default_resolution(db_session):
    policies = await resolve_deployment_policies(db_session)
    assert policies.max_concurrent_per_user == DEFAULTS["max_concurrent_per_user"]
    assert policies.max_ttl_hours == DEFAULTS["max_ttl_hours"]
    assert policies.allowed_regions == [policies_svc.DEPLOY_REGION]


async def test_policies_stored_overrides_merge(db_session):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"max_concurrent_per_user": 1, "default_ttl_hours": 24},
        )
    )
    await db_session.commit()
    policies = await resolve_deployment_policies(db_session)
    assert policies.max_concurrent_per_user == 1
    assert policies.default_ttl_hours == 24
    assert policies.max_ttl_hours == DEFAULTS["max_ttl_hours"]  # untouched key


async def test_validate_policies_rejections():
    with pytest.raises(ValueError):
        validate_policies({"max_concurrent_per_user": 0})
    with pytest.raises(ValueError):
        validate_policies({"max_ttl_hours": "week"})
    with pytest.raises(ValueError):
        validate_policies({"default_ttl_hours": 200, "max_ttl_hours": 100})
    with pytest.raises(ValueError):
        validate_policies({"allowed_regions": []})
    with pytest.raises(ValueError):
        validate_policies({"per_deployment_budget_usd": -5})
    validate_policies(dict(DEFAULTS))  # defaults are self-consistent


async def _project(db, user, name="P") -> Project:
    project = Project(user_id=user.id, name=name, status="draft")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _deployment(db, user, project, status="active") -> Deployment:
    row = Deployment(
        project_id=project.id, user_id=user.id, status=status, timeline=[], resources=[]
    )
    db.add(row)
    await db.commit()
    return row


async def test_preflight_user_concurrency(db_session, test_user):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"max_concurrent_per_user": 2},
        )
    )
    for i in range(2):
        await _deployment(db_session, test_user, await _project(db_session, test_user, f"P{i}"))
    with pytest.raises(PolicyViolation) as exc:
        await enforce_deploy_preflight(db_session, test_user.id, is_update=False)
    assert exc.value.code == "policy_concurrency_user"
    # Updates replace a running stack — never counted
    policies = await enforce_deploy_preflight(db_session, test_user.id, is_update=True)
    assert policies.max_concurrent_per_user == 2


async def test_preflight_platform_concurrency(db_session, test_user, admin_user):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"max_concurrent_per_user": 10, "max_concurrent_platform": 1},
        )
    )
    await _deployment(db_session, admin_user, await _project(db_session, admin_user, "A"))
    with pytest.raises(PolicyViolation) as exc:
        await enforce_deploy_preflight(db_session, test_user.id, is_update=False)
    assert exc.value.code == "policy_concurrency_platform"


async def test_preflight_ignores_terminal_states(db_session, test_user):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"max_concurrent_per_user": 1},
        )
    )
    for status in ("failed", "torn_down", "superseded"):
        await _deployment(
            db_session, test_user,
            await _project(db_session, test_user, f"P-{status}"), status=status,
        )
    policies = await enforce_deploy_preflight(db_session, test_user.id, is_update=False)
    assert policies.max_concurrent_per_user == 1  # no violation raised


async def test_preflight_region_disabled(db_session, test_user, monkeypatch):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"allowed_regions": ["eu-west-1"]},
        )
    )
    await db_session.commit()
    with pytest.raises(PolicyViolation) as exc:
        await enforce_deploy_preflight(db_session, test_user.id, is_update=False)
    assert exc.value.code == "policy_region"


async def test_deploy_endpoint_maps_policy_409(client, db_session, test_user, audit_db):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"max_concurrent_per_user": 1},
        )
    )
    await _deployment(db_session, test_user, await _project(db_session, test_user, "Live"))
    fresh = await _project(db_session, test_user, "Blocked")
    resp = await client.post(f"/api/v1/projects/{fresh.id}/deploy")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "policy_concurrency_user"


async def test_deploy_endpoint_maps_region_422(client, db_session, test_user, audit_db):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"allowed_regions": ["eu-central-1"]},
        )
    )
    project = await _project(db_session, test_user)
    resp = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "policy_region"


async def test_resolve_ttl_hours_reads_policies(db_session, audit_db):
    from app.services.deployment import resolve_ttl_hours

    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"default_ttl_hours": 12, "max_ttl_hours": 48},
        )
    )
    await db_session.commit()
    assert await resolve_ttl_hours(db_session) == (12, 48)


async def test_admin_settings_roundtrip_and_validation(admin_user, client_for, db_session, audit_db):
    db_session.add(
        PlatformSettings(id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={})
    )
    await db_session.commit()
    async with client_for(admin_user) as ac:
        current = (await ac.get("/api/v1/admin/model-controls")).json()
        body = {
            "model_allowlist": ["us.anthropic.claude-sonnet-5"],
            "param_bounds": current["param_bounds"],
            "rate_limits": current["rate_limits"],
            "cost": current["cost"],
            "deployment_policies": {"max_concurrent_per_user": 5, "default_ttl_hours": 24},
        }
        updated = (await ac.put("/api/v1/admin/model-controls", json=body)).json()
        assert updated["deployment_policies"]["max_concurrent_per_user"] == 5

        bad = dict(body, deployment_policies={"default_ttl_hours": 999})
        resp = await ac.put("/api/v1/admin/model-controls", json=bad)
        assert resp.status_code == 422  # merged default_ttl > max_ttl

        unknown = dict(body, deployment_policies={"max_deploys": 1})
        assert (await ac.put("/api/v1/admin/model-controls", json=unknown)).status_code == 422


# ------------------------------------------------------ shared-state counters


async def test_shared_counters_contention():
    from app.services.shared_state import STATE

    async def bump100():
        for _ in range(100):
            await STATE.add_and_get("contended", 1)

    await asyncio.gather(*[bump100() for _ in range(5)])
    assert await STATE.get("contended") == 500


async def test_seed_if_absent_is_idempotent():
    from app.services.shared_state import STATE

    await asyncio.gather(*[STATE.seed_if_absent("seeded", 400) for _ in range(5)])
    assert await STATE.get("seeded") == 400
    await STATE.seed_if_absent("seeded", 999)  # later seeds never overwrite
    assert await STATE.get("seeded") == 400


async def test_counter_ttl_expiry(monkeypatch):
    import time as time_mod

    from app.services.shared_state import STATE

    await STATE.add_and_get("ephemeral", 7, ttl_s=1)
    assert await STATE.get("ephemeral") == 7
    real_time = time_mod.time
    monkeypatch.setattr(time_mod, "time", lambda: real_time() + 5)
    assert await STATE.get("ephemeral") == 0


# ------------------------------------------------------------------ relay


class _FakeConn:
    def __init__(self):
        self.notifies: list[tuple] = []

    async def execute(self, _sql, *args):
        self.notifies.append(args)


async def test_relay_mirrors_publish_and_self_skips():
    import json

    from app.services import event_relay as relay_mod
    from app.services.deployment import DeploymentEventBus

    relay = relay_mod.EventRelay()
    bus = DeploymentEventBus()
    relay.wire("test", bus)
    relay._conn = _FakeConn()

    queue = await bus.subscribe("k1")
    await bus.publish("k1", {"type": "phase", "detail": "x"})
    # Local subscriber sees it AND one NOTIFY went out
    assert (await asyncio.wait_for(queue.get(), 1))["type"] == "phase"
    assert len(relay._conn.notifies) == 1
    payload = json.loads(relay._conn.notifies[0][1])
    assert payload["bus"] == "test" and payload["src"] == relay_mod.TASK_ID

    # Inbound: our own notification is skipped
    relay._on_notify(None, None, relay_mod.CHANNEL, relay._conn.notifies[0][1])
    await asyncio.sleep(0.05)
    assert queue.empty()

    # Inbound from a peer task dispatches locally WITHOUT re-notifying
    foreign = json.dumps(
        {"src": "peer-task", "bus": "test", "key": "k1", "event": {"type": "cfn"}}
    )
    relay._on_notify(None, None, relay_mod.CHANNEL, foreign)
    event = await asyncio.wait_for(queue.get(), 1)
    assert event["type"] == "cfn"
    assert len(relay._conn.notifies) == 1  # no loop


async def test_relay_oversized_event_stays_local():
    from app.services import event_relay as relay_mod
    from app.services.deployment import DeploymentEventBus

    relay = relay_mod.EventRelay()
    bus = DeploymentEventBus()
    relay.wire("big", bus)
    relay._conn = _FakeConn()
    queue = await bus.subscribe("k")
    await bus.publish("k", {"blob": "x" * 9000})
    assert (await asyncio.wait_for(queue.get(), 1))["blob"]  # local delivery intact
    assert relay._conn.notifies == []  # nothing relayed


async def test_relay_down_never_breaks_publish():
    from app.services import event_relay as relay_mod
    from app.services.deployment import DeploymentEventBus

    relay = relay_mod.EventRelay()
    bus = DeploymentEventBus()
    relay.wire("down", bus)
    assert relay._conn is None  # no connection at all
    queue = await bus.subscribe("k")
    await bus.publish("k", {"type": "phase"})  # must not raise
    assert (await asyncio.wait_for(queue.get(), 1))["type"] == "phase"


# ---------------------------------------------------------------- scheduler


async def test_window_fence_admits_exactly_one():
    """Racing ticks in one interval window: first owns it, rest skip."""
    from app.services.scheduler import _window_fenced

    results = await asyncio.gather(*[_window_fenced("drill", 900) for _ in range(4)])
    assert results.count(True) == 1 and results.count(False) == 3


async def test_election_always_runs_on_sqlite(db_engine, monkeypatch):
    """Non-postgres = single task: every tick is elected."""
    import app.core.db as core_db
    from app.services.scheduler import _run_elected

    monkeypatch.setattr(core_db, "engine", db_engine)
    ran = []

    async def tick():
        ran.append(1)

    assert await _run_elected(999, tick) is True
    assert ran == [1]


# ------------------------------------------------------------------ tenancy


async def _tenant_b(db) -> None:
    from app.models import Tenant

    if await db.get(Tenant, TENANT_B) is None:
        db.add(Tenant(id=TENANT_B, name="Second Tenant"))
        await db.commit()


async def test_projects_are_tenant_isolated(client, db_session, test_user, audit_db):
    await _tenant_b(db_session)
    mine = await _project(db_session, test_user, "Mine")
    foreign = Project(
        user_id=test_user.id, name="Foreign", status="draft", tenant_id=TENANT_B
    )
    db_session.add(foreign)
    await db_session.commit()

    listing = (await client.get("/api/v1/projects")).json()
    names = [p["name"] for p in listing["items"]]
    assert "Mine" in names and "Foreign" not in names

    # Direct fetch cross-tenant → invisible (404), even for the owner
    assert (await client.get(f"/api/v1/projects/{foreign.id}")).status_code == 404
    assert (await client.get(f"/api/v1/projects/{mine.id}")).status_code == 200


async def test_marketplace_tenant_isolated(client, db_session, test_user, audit_db):
    from app.models import MarketplaceSample

    await _tenant_b(db_session)
    db_session.add(
        MarketplaceSample(
            title="Ours", description="s", category="ops", complexity="beginner",
            status="published", author_id=test_user.id,
        )
    )
    db_session.add(
        MarketplaceSample(
            title="Theirs", description="s", category="ops", complexity="beginner",
            status="published", author_id=test_user.id, tenant_id=TENANT_B,
        )
    )
    await db_session.commit()
    data = (await client.get("/api/v1/marketplace/samples")).json()
    titles = [s["title"] for s in data["items"]]
    assert "Ours" in titles and "Theirs" not in titles


async def test_notifications_tenant_isolated(client, db_session, test_user, audit_db):
    from app.models import Notification

    await _tenant_b(db_session)
    db_session.add(Notification(user_id=test_user.id, type="generation_done", title="ours", body="b"))
    db_session.add(
        Notification(
            user_id=test_user.id, type="generation_done", title="theirs", body="b",
            tenant_id=TENANT_B,
        )
    )
    await db_session.commit()
    data = (await client.get("/api/v1/notifications")).json()
    titles = [n["title"] for n in data["items"]]
    assert "ours" in titles and "theirs" not in titles


async def test_audit_log_tenant_isolated(admin_user, client_for, db_session, audit_db):
    from app.models import AuditLog

    await _tenant_b(db_session)
    db_session.add(AuditLog(actor_id=admin_user.id, action="ours.action", category="admin"))
    db_session.add(
        AuditLog(
            actor_id=admin_user.id, action="theirs.action", category="admin",
            tenant_id=TENANT_B,
        )
    )
    await db_session.commit()
    async with client_for(admin_user) as ac:
        data = (await ac.get("/api/v1/admin/audit-logs")).json()
    actions = [e["action"] for e in data["items"]]
    assert any(a == "ours.action" for a in actions)
    assert all(a != "theirs.action" for a in actions)


async def test_seed_tenant_default_applies(db_session, test_user):
    """TenantMixin default stamps the platform tenant on every insert."""
    project = await _project(db_session, test_user, "Stamped")
    row = (
        await db_session.execute(select(Project).where(Project.id == project.id))
    ).scalar_one()
    assert row.tenant_id == PLATFORM_TENANT_ID
