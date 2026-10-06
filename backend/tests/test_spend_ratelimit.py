"""Spend tracker, cost caps, rate limits (cost-caps-alerts spec)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import Alert, ModelInvocation, Notification, PlatformSettings, Project
from app.services import spend as spend_svc
from app.services.ratelimit import RateLimited, TokenBuckets
from app.services.spend import (
    CostCapExceeded,
    EffectiveCaps,
    SpendTracker,
    resolve_caps,
)

pytestmark = pytest.mark.asyncio

CAPS = EffectiveCaps(
    user_cap=Decimal("10"), project_cap=Decimal("20"), platform_cap=Decimal("100"),
    thresholds=[50, 75, 90, 100], at_cap="block",
)


def fresh_tracker() -> SpendTracker:
    tracker = SpendTracker()
    tracker.month = datetime.now(UTC).strftime("%Y-%m")
    tracker.hydrated = True
    return tracker


# ------------------------------------------------------------------- tracker


async def test_add_detects_threshold_crossings():
    tracker = fresh_tracker()
    user_id, project_id = uuid.uuid4(), uuid.uuid4()
    crossings = await tracker.add(user_id, project_id, Decimal("4.9"), CAPS)
    assert crossings == []  # 4.9/10 = 49%
    crossings = await tracker.add(user_id, project_id, Decimal("0.2"), CAPS)
    assert [(c.scope, c.threshold_pct) for c in crossings] == [("user", 50)]  # 51%
    crossings = await tracker.add(user_id, project_id, Decimal("5.0"), CAPS)
    pcts = [(c.scope, c.threshold_pct) for c in crossings]
    assert ("user", 75) in pcts and ("user", 90) in pcts and ("user", 100) in pcts
    assert ("project", 50) in pcts  # 10.1/20 = 50.5%


async def test_check_blocks_at_cap_and_exemptions():
    tracker = fresh_tracker()
    user_id = uuid.uuid4()
    tracker.user_mtd[user_id] = Decimal("10")
    with pytest.raises(CostCapExceeded) as exc:
        await tracker.check(user_id, None, CAPS, exempt_user_scopes=False)
    assert exc.value.scope == "user"
    # Exempt purposes skip user/project scopes
    await tracker.check(user_id, None, CAPS, exempt_user_scopes=True)
    # Platform cap still applies to exempt purposes
    tracker.platform_mtd = Decimal("100")
    with pytest.raises(CostCapExceeded) as exc:
        await tracker.check(user_id, None, CAPS, exempt_user_scopes=True)
    assert exc.value.scope == "platform"


async def test_check_alert_mode_never_blocks():
    tracker = fresh_tracker()
    user_id = uuid.uuid4()
    tracker.user_mtd[user_id] = Decimal("999")
    caps = EffectiveCaps(
        user_cap=Decimal("10"), project_cap=None, platform_cap=None,
        thresholds=[100], at_cap="alert",
    )
    await tracker.check(user_id, None, caps, exempt_user_scopes=False)  # no raise


async def test_unhydrated_tracker_fails_open():
    tracker = SpendTracker()
    tracker.month = datetime.now(UTC).strftime("%Y-%m")
    tracker.hydrated = False
    with patch.object(tracker, "hydrate") as hydrate:
        hydrate.return_value = None
        await tracker.check(uuid.uuid4(), None, CAPS, exempt_user_scopes=False)  # no raise


async def test_hydrate_from_invocations(db_session, test_user, audit_db):
    project = Project(user_id=test_user.id, name="P", status="draft")
    db_session.add(project)
    await db_session.flush()
    for cost in ("1.50", "2.50"):
        db_session.add(
            ModelInvocation(
                user_id=test_user.id, project_id=project.id, purpose="chat",
                model_id="m", prompt_text="p", prompt_sha256="0" * 64,
                response_text="r", input_tokens=1, output_tokens=1,
                latency_ms=1, cost_usd=cost, created_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    tracker = SpendTracker()
    await tracker.hydrate()
    assert tracker.hydrated
    assert tracker.user_mtd[test_user.id] == Decimal("4.00")
    assert tracker.project_mtd[project.id] == Decimal("4.00")
    assert tracker.platform_mtd == Decimal("4.00")


# ------------------------------------------------------------ caps resolution


async def test_resolve_caps_precedence(db_session, test_user):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={},
            cost={"default_user_cap_usd": 500, "default_project_cap_usd": 1000,
                  "platform_budget_usd": None, "alert_thresholds": [50, 100], "at_cap": "block"},
        )
    )
    project = Project(user_id=test_user.id, name="B", status="draft")
    db_session.add(project)
    await db_session.commit()

    caps = await resolve_caps(db_session, test_user.id, project.id)
    assert caps.user_cap == Decimal("500") and caps.project_cap == Decimal("1000")
    assert caps.platform_cap is None and caps.at_cap == "block"

    test_user.budget_override_usd = 50
    project.budget_override_usd = 75
    await db_session.commit()
    caps = await resolve_caps(db_session, test_user.id, project.id)
    assert caps.user_cap == Decimal("50") and caps.project_cap == Decimal("75")


# ----------------------------------------------------- crossings → alerts


async def test_record_crossings_dedupes_and_notifies(db_session, test_user, admin_user, audit_db):
    crossing = spend_svc.Crossing(
        scope="user", scope_id=test_user.id, threshold_pct=75,
        cap=Decimal("100"), mtd=Decimal("76"),
    )
    await spend_svc.record_crossings(db_session, [crossing])
    await spend_svc.record_crossings(db_session, [crossing])  # monthly dedupe
    # Column selects: rollback inside the dedupe path expires ORM instances
    alerts = (await db_session.execute(select(Alert.threshold_pct, Alert.severity))).all()
    assert alerts == [(75, "info")]
    notif_users = (await db_session.execute(select(Notification.user_id))).scalars().all()
    assert notif_users == [test_user.id]

    critical = spend_svc.Crossing(
        scope="user", scope_id=test_user.id, threshold_pct=100,
        cap=Decimal("100"), mtd=Decimal("101"),
    )
    await spend_svc.record_crossings(db_session, [critical])
    severities = set((await db_session.execute(select(Alert.severity))).scalars().all())
    assert severities == {"info", "critical"}
    # 100% also notifies admins
    recipient_ids = set((await db_session.execute(select(Notification.user_id))).scalars().all())
    assert admin_user.id in recipient_ids


# ---------------------------------------------------------------- rate limits


async def test_bucket_math_and_retry_after(monkeypatch):
    from app.services.platform_settings import ModelControls

    controls = ModelControls(
        allowlist=["m"], temperature_min=0, temperature_max=1,
        top_p_min=0, top_p_max=1, max_tokens=8192, max_context=200000,
        rate_limits={"per_user_rpm": 2, "platform_rpm": 1000},
    )

    async def fake_controls():
        return controls

    monkeypatch.setattr("app.services.platform_settings.get_controls", fake_controls)
    buckets = TokenBuckets()
    user_id = uuid.uuid4()
    await buckets.acquire(user_id, None, max_wait_s=0.05)
    await buckets.acquire(user_id, None, max_wait_s=0.05)
    with pytest.raises(RateLimited) as exc:
        await buckets.acquire(user_id, None, max_wait_s=0.05)
    assert exc.value.scope == "user" and exc.value.retry_after_s >= 1


async def test_window_rollover_admits_after_wait(monkeypatch):
    """§4.6.7 queueing under S12 fixed windows: a drained window queues the
    caller, and the next window admits it within max_wait."""
    from app.services.platform_settings import ModelControls

    controls = ModelControls(
        allowlist=["m"], temperature_min=0, temperature_max=1,
        top_p_min=0, top_p_max=1, max_tokens=8192, max_context=200000,
        rate_limits={"per_user_rpm": 2},
    )

    async def fake_controls():
        return controls

    monkeypatch.setattr("app.services.platform_settings.get_controls", fake_controls)
    monkeypatch.setattr("app.services.ratelimit.WINDOW_S", 1)  # fast rollover
    buckets = TokenBuckets()
    user_id = uuid.uuid4()
    await buckets.acquire(user_id, None, max_wait_s=0.05)
    await buckets.acquire(user_id, None, max_wait_s=0.05)
    # Window drained; queueing past the rollover succeeds (§4.6.7)
    await buckets.acquire(user_id, None, max_wait_s=2.5)


async def test_rate_settings_failure_fails_open(monkeypatch):
    async def broken():
        raise RuntimeError("settings down")

    monkeypatch.setattr("app.services.platform_settings.get_controls", broken)
    buckets = TokenBuckets()
    await buckets.acquire(uuid.uuid4(), None, max_wait_s=0.01)  # no raise


# ------------------------------------------------------------ APIs


async def test_alerts_api_and_acknowledge(admin_user, client_for, db_session, audit_db):
    db_session.add(
        Alert(kind="cost_user", severity="warning", message="90% used",
              threshold_pct=90, month="2026-07")
    )
    await db_session.commit()
    async with client_for(admin_user) as ac:
        active = (await ac.get("/api/v1/admin/alerts")).json()
        assert active["total"] == 1
        alert_id = active["items"][0]["id"]
        acked = (await ac.put(f"/api/v1/admin/alerts/{alert_id}/acknowledge")).json()
        assert acked["status"] == "acknowledged"
        assert (await ac.get("/api/v1/admin/alerts")).json()["total"] == 0
        assert (await ac.get("/api/v1/admin/alerts?status=all")).json()["total"] == 1


async def test_project_budget_put_and_usage(client, db_session, test_user, audit_db):
    project = Project(user_id=test_user.id, name="Budgeted", status="draft")
    db_session.add(project)
    await db_session.commit()

    set_resp = await client.put(
        f"/api/v1/projects/{project.id}/budget", json={"budget_override_usd": 42.5}
    )
    assert set_resp.status_code == 200 and set_resp.json()["budget_override_usd"] == 42.5
    bad = await client.put(
        f"/api/v1/projects/{project.id}/budget", json={"budget_override_usd": -5}
    )
    assert bad.status_code == 422
    cleared = await client.put(
        f"/api/v1/projects/{project.id}/budget", json={"budget_override_usd": None}
    )
    assert cleared.json()["budget_override_usd"] is None

    db_session.add(
        ModelInvocation(
            user_id=test_user.id, project_id=project.id, purpose="chat", model_id="m",
            prompt_text="p", prompt_sha256="0" * 64, response_text="r",
            input_tokens=1, output_tokens=1, latency_ms=1, cost_usd="1.25",
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    usage = (await client.get("/api/v1/users/me/usage")).json()
    assert usage["mtd_usd"] == pytest.approx(1.25)
    assert usage["projects"][0]["name"] == "Budgeted"
