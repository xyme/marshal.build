"""Deployment maturity tests (deployment-maturity spec): in-place updates,
health ticks, expiry sweeps, extend clamps, history."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import Alert, Deployment, Lease, Notification, Project, User
from app.services import deployment as deploy_service
from app.services import deployment_lifecycle as lifecycle

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def project(db_session, test_user):
    row = Project(user_id=test_user.id, name="Maturity", status="deployed")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _swallow(_id, coro):
    coro.close()


class FakeCfn:
    """Scriptable CloudFormation client for the update path."""

    def __init__(self, statuses: list[str], *, update_error: Exception | None = None):
        self.statuses = list(statuses)
        self.update_error = update_error
        self.update_calls: list[dict] = []

    def update_stack(self, **kwargs):
        if self.update_error:
            raise self.update_error
        self.update_calls.append(kwargs)

    def describe_stacks(self, StackName):  # noqa: N803
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Stacks": [{"StackStatus": status,
                            "Outputs": [{"OutputKey": "ApiUrl", "OutputValue": "https://u"}]}]}

    def describe_stack_events(self, StackName):  # noqa: N803
        return {"StackEvents": []}


class FakeProvider:
    name = "direct"

    def deployment_session(self, lease_info):
        class S:
            def __init__(self, cfn):
                self._cfn = cfn

            def client(self, kind):
                return self._cfn

        return S(self.cfn)

    async def lease_state(self, ref):
        return {"budget_used_usd": 5, "budget_cap_usd": 1000,
                "expires_at": (datetime.now(UTC) + timedelta(days=20)).isoformat()}


async def _active_pair(db, user, project) -> tuple[Deployment, Deployment]:
    """(active prior, pending update attempt) sharing a stack + lease."""
    lease = Lease(provider="direct", external_lease_id="lease-1", aws_account_id="123456789012",
                  status="active", project_id=project.id, user_id=user.id)
    db.add(lease)
    await db.flush()
    prior = Deployment(
        project_id=project.id, user_id=user.id, status="active",
        stack_name="marshal-x", stack_id="arn:x", lease_id=lease.id,
        app_url="https://old", timeline=[], resources=[],
        deployed_at=datetime.now(UTC),
    )
    db.add(prior)
    await db.flush()
    attempt = Deployment(
        project_id=project.id, user_id=user.id, status="pending",
        stack_name="marshal-x", stack_id="arn:x", lease_id=lease.id,
        timeline=[], resources=[],
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(prior)
    await db.refresh(attempt)
    return prior, attempt


async def _run_update_with(db, monkeypatch, cfn: FakeCfn, attempt, prior):
    provider = FakeProvider()
    provider.cfn = cfn
    monkeypatch.setattr(deploy_service, "get_sandbox_provider", lambda: provider)
    monkeypatch.setattr(deploy_service, "POLL_INTERVAL_S", 0.01)
    await deploy_service._run_update(attempt.id, prior.id)
    await db.refresh(attempt)
    await db.refresh(prior)


# ------------------------------------------------------------- update path


async def test_update_success_supersedes_prior(db_session, test_user, project, audit_db, monkeypatch):
    prior, attempt = await _active_pair(db_session, test_user, project)
    cfn = FakeCfn(["UPDATE_IN_PROGRESS", "UPDATE_COMPLETE"])
    await _run_update_with(db_session, monkeypatch, cfn, attempt, prior)
    assert attempt.status == "active"
    assert attempt.expires_at is not None  # fresh TTL (R5.1)
    assert prior.status == "superseded" and prior.superseded_at is not None
    assert cfn.update_calls  # update_stack really ran


async def test_update_rollback_restores_prior(db_session, test_user, project, audit_db, monkeypatch):
    prior, attempt = await _active_pair(db_session, test_user, project)
    cfn = FakeCfn(["UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_COMPLETE"])
    await _run_update_with(db_session, monkeypatch, cfn, attempt, prior)
    assert attempt.status == "failed"
    last_detail = (attempt.timeline or [])[-1].get("detail") or ""
    assert "rolled back" in last_detail.lower()
    assert prior.status == "active"  # the record mirrors the running stack (R2.1)


async def test_update_rollback_failed_alerts_admins(db_session, test_user, project, audit_db, monkeypatch):
    prior, attempt = await _active_pair(db_session, test_user, project)
    cfn = FakeCfn(["UPDATE_IN_PROGRESS", "UPDATE_ROLLBACK_FAILED"])
    await _run_update_with(db_session, monkeypatch, cfn, attempt, prior)
    assert attempt.status == "failed"
    assert prior.health == "degraded"
    kinds = (await db_session.execute(select(Alert.kind))).scalars().all()
    assert "sandbox" in kinds  # critical stuck-stack alert (R2.4)


async def test_update_noop_fails_fast(db_session, test_user, project, audit_db, monkeypatch):
    from botocore.exceptions import ClientError

    prior, attempt = await _active_pair(db_session, test_user, project)
    err = ClientError(
        {"Error": {"Code": "ValidationError",
                   "Message": "No updates are to be performed."}}, "UpdateStack",
    )
    cfn = FakeCfn(["UPDATE_COMPLETE"], update_error=err)
    await _run_update_with(db_session, monkeypatch, cfn, attempt, prior)
    assert attempt.status == "failed"
    assert "No changes to deploy" in (attempt.error or "")
    assert prior.status == "active"


# ------------------------------------------------------------- health tick


async def test_health_tick_three_strikes_then_recovery(db_session, test_user, project, audit_db, monkeypatch):
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        app_url="https://app.example", health="healthy", timeline=[], resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()

    probe_results = {"value": (False, 500)}

    async def fake_probe(url):
        return probe_results["value"]

    monkeypatch.setattr(lifecycle, "_probe", fake_probe)

    for _strike in range(3):
        await lifecycle.health_tick()
    await db_session.refresh(deployment)
    assert deployment.health == "degraded"
    notif_types = (
        (await db_session.execute(select(Notification.type))).scalars().all()
    )
    assert notif_types.count("deployment_degraded") == 1  # one incident, one notify

    # extra failing ticks do NOT re-notify (incident-scoped dedupe)
    await lifecycle.health_tick()
    notif_types = (
        (await db_session.execute(select(Notification.type))).scalars().all()
    )
    assert notif_types.count("deployment_degraded") == 1

    # recovery flips health back without notification spam (R1.3)
    probe_results["value"] = (True, 200)
    await lifecycle.health_tick()
    await db_session.refresh(deployment)
    assert deployment.health == "healthy"
    assert any(e.get("phase") == "health_recovered" for e in deployment.timeline)


# ------------------------------------------------------------- expiry sweep


async def test_expiry_warnings_and_teardown(db_session, test_user, project, audit_db, monkeypatch):
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        app_url="https://app.example", timeline=[], resources=[],
        deployed_at=datetime.now(UTC) - timedelta(hours=50),
        expires_at=datetime.now(UTC) + timedelta(hours=23),  # inside the 24h window
    )
    db_session.add(deployment)
    await db_session.commit()

    await lifecycle.expiry_tick()
    await lifecycle.expiry_tick()  # double-fire: dedupe holds
    notif_types = (
        (await db_session.execute(select(Notification.type))).scalars().all()
    )
    assert notif_types.count("deployment_expiring") == 1

    # cross the expiry line → standard teardown path is invoked (R5.4)
    deployment.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    with patch.object(deploy_service, "_spawn", side_effect=_swallow):
        await lifecycle.expiry_tick()
    await db_session.refresh(deployment)
    assert deployment.status == "tearing_down"
    notif_types = (
        (await db_session.execute(select(Notification.type))).scalars().all()
    )
    assert "deployment_expired" in notif_types


async def test_legacy_null_expiry_grandfathered(db_session, test_user, project, audit_db):
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        app_url="https://a", timeline=[], resources=[], expires_at=None,
    )
    db_session.add(deployment)
    await db_session.commit()
    await lifecycle.expiry_tick()
    await db_session.refresh(deployment)
    assert deployment.status == "active"  # never auto-expired


# ------------------------------------------------------------- extend + history


async def test_extend_clamps_and_audits(db_session, test_user, project, client_for, audit_db, monkeypatch):
    lease = Lease(provider="direct", external_lease_id="lease-9", aws_account_id="123456789012",
                  status="active", project_id=project.id, user_id=test_user.id)
    db_session.add(lease)
    await db_session.flush()
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        stack_name="marshal-y", lease_id=lease.id, timeline=[], resources=[],
        deployed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=72),
    )
    db_session.add(deployment)
    await db_session.commit()

    provider = FakeProvider()
    import app.api.projects as projects_api
    import app.services.sandbox as sandbox_pkg

    monkeypatch.setattr(sandbox_pkg, "get_sandbox_provider", lambda: provider)
    projects_api._lease_state_cache.clear()

    async with client_for(test_user) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/deployment/extend", json={"hours": 24}
        )
        assert r.status_code == 200, r.text
        # beyond max TTL (deployed_at + 168h) → 422
        r = await client.post(
            f"/api/v1/projects/{project.id}/deployment/extend", json={"hours": 168}
        )
        assert r.status_code == 422

    viewer = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="mviewer@marshal.demo",
        role="power", persona="power", onboarding_completed=True,
    )
    db_session.add(viewer)
    await db_session.commit()
    from app.models import ProjectMember

    db_session.add(ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer"))
    await db_session.commit()
    async with client_for(viewer) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/deployment/extend", json={"hours": 4}
        )
        assert r.status_code == 403  # owner-only

        # history is member-visible (R4.1)
        r = await client.get(f"/api/v1/projects/{project.id}/deployments")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1 and rows[0]["health"] in ("unknown", "healthy")


# ------------------------------------------------------- changeset preview


class FakeCfnPreview:
    """Scriptable change-set client: describe pages then terminal status."""

    def __init__(self, status="CREATE_COMPLETE", reason="", changes=None, pages=None):
        self.status = status
        self.reason = reason
        self.changes = changes or []
        self.extra_pages = pages or []
        self.created: list[dict] = []
        self.deleted: list[dict] = []

    def create_change_set(self, **kwargs):
        self.created.append(kwargs)

    def describe_change_set(self, **kwargs):
        if "NextToken" in kwargs:
            page = self.extra_pages.pop(0)
            return {"Status": self.status, "Changes": page,
                    "NextToken": "t" if self.extra_pages else None}
        return {
            "Status": self.status,
            "StatusReason": self.reason,
            "Changes": self.changes,
            "NextToken": "t" if self.extra_pages else None,
        }

    def delete_change_set(self, **kwargs):
        self.deleted.append(kwargs)


async def _preview_with(db, monkeypatch, cfn, project, build=None):
    provider = FakeProvider()
    provider.cfn = cfn
    monkeypatch.setattr(deploy_service, "get_sandbox_provider", lambda: provider)
    return await deploy_service.preview_update(db, project, build)


async def test_preview_maps_changes_and_cleans_up(
    db_session, test_user, project, audit_db, monkeypatch
):
    await _active_pair(db_session, test_user, project)
    cfn = FakeCfnPreview(
        changes=[
            {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "Fn",
                                "ResourceType": "AWS::Lambda::Function",
                                "Replacement": "False", "Scope": ["Properties"]}},
        ],
        pages=[[{"ResourceChange": {"Action": "Add", "LogicalResourceId": "Tbl",
                                     "ResourceType": "AWS::DynamoDB::Table"}}]],
    )
    result = await _preview_with(db_session, monkeypatch, cfn, project)
    assert result["no_changes"] is False
    assert [c["logical_id"] for c in result["changes"]] == ["Fn", "Tbl"]
    assert result["changes"][0]["replacement"] == "False"
    assert cfn.created and cfn.created[0]["ChangeSetType"] == "UPDATE"
    assert cfn.deleted, "preview must delete its change set"


async def test_preview_no_changes(db_session, test_user, project, audit_db, monkeypatch):
    await _active_pair(db_session, test_user, project)
    cfn = FakeCfnPreview(
        status="FAILED",
        reason="The submitted information didn't contain changes.",
    )
    result = await _preview_with(db_session, monkeypatch, cfn, project)
    assert result["no_changes"] is True and result["changes"] == []
    assert cfn.deleted  # cleanup still happens


async def test_preview_requires_active_deployment(
    db_session, test_user, project, audit_db, monkeypatch
):
    cfn = FakeCfnPreview()
    with pytest.raises(ValueError, match="No active deployment"):
        await _preview_with(db_session, monkeypatch, cfn, project)


async def test_preview_surfaces_real_failures(
    db_session, test_user, project, audit_db, monkeypatch
):
    await _active_pair(db_session, test_user, project)
    cfn = FakeCfnPreview(status="FAILED", reason="Access denied on role")
    with pytest.raises(ValueError, match="Change set failed"):
        await _preview_with(db_session, monkeypatch, cfn, project)
    assert cfn.deleted
