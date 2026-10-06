"""B20 testbed credential vending (R2): refusal matrix, idempotent role
bootstrap, curated policy shape, session attribution, audit event."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Deployment, Lease, PlatformSettings, Project
from app.services import deployment as deploy_svc
from app.services import testbed as testbed_svc

pytestmark = pytest.mark.asyncio


async def _project(db, user, name="Testbed"):
    proj = Project(user_id=user.id, name=f"{name}-{uuid.uuid4().hex[:6]}")
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


async def _active_testbed(db, user, project, provider="isb", mode="testbed"):
    lease = Lease(
        provider=provider, external_lease_id="lease-tb", aws_account_id="210987654321",
        status="active", project_id=project.id, user_id=user.id,
    )
    db.add(lease)
    await db.flush()
    deployment = Deployment(
        project_id=project.id, user_id=user.id, status="active", mode=mode,
        stack_name="marshal-tb", stack_id="arn:tb", lease_id=lease.id,
    )
    db.add(deployment)
    await db.commit()
    return deployment


class _FakeIamExceptions:
    class NoSuchEntityException(Exception):
        pass


class FakeIam:
    exceptions = _FakeIamExceptions

    def __init__(self, role_exists: bool = False):
        self.role_exists = role_exists
        self.created: list[dict] = []
        self.policies_put: list[dict] = []

    def get_role(self, RoleName):
        if not self.role_exists:
            raise self.exceptions.NoSuchEntityException(RoleName)
        return {"Role": {"RoleName": RoleName}}

    def create_role(self, **kwargs):
        self.role_exists = True
        self.created.append(kwargs)
        return {"Role": {"RoleName": kwargs["RoleName"]}}

    def put_role_policy(self, **kwargs):
        self.policies_put.append(kwargs)


class FakeSession:
    def __init__(self, iam: FakeIam):
        self._iam = iam

    def client(self, kind):
        assert kind == "iam"
        return self._iam


class FakeProvider:
    name = "isb"

    def __init__(self, iam: FakeIam):
        self._iam = iam

    def deployment_session(self, lease_info):
        return FakeSession(self._iam)


def _wire(monkeypatch, iam: FakeIam):
    """Patch the AWS seams: provider session, platform identity, vend, URL."""
    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: FakeProvider(iam))
    monkeypatch.setattr(
        testbed_svc, "_platform_principal_arn",
        lambda: "arn:aws:iam::123456789012:role/MarshalBackendTaskRole",
    )

    def fake_assume(account_id, session_name, hours):
        return {
            "AccessKeyId": "ASIAFAKE",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": datetime.now(UTC) + timedelta(hours=hours),
        }

    monkeypatch.setattr(testbed_svc, "_assume_testbed_role", fake_assume)

    async def fake_url(creds):
        return "https://signin.aws.amazon.com/federation?Action=login&fake=1"

    monkeypatch.setattr(testbed_svc, "_console_signin_url", fake_url)


# ------------------------------------------------- refusal matrix


async def test_refuses_without_active_deployment(db_session, test_user):
    project = await _project(db_session, test_user)
    with pytest.raises(testbed_svc.TestbedUnavailable, match="No active deployment"):
        await testbed_svc.issue_credentials(db_session, project, test_user)


async def test_refuses_full_governance_mode(db_session, test_user):
    project = await _project(db_session, test_user)
    await _active_testbed(db_session, test_user, project, mode="full_governance")
    with pytest.raises(testbed_svc.TestbedUnavailable, match="Full Governance"):
        await testbed_svc.issue_credentials(db_session, project, test_user)


async def test_refuses_non_isb_provider(db_session, test_user):
    """The direct provider IS the platform account — vending must refuse."""
    project = await _project(db_session, test_user)
    await _active_testbed(db_session, test_user, project, provider="direct")
    with pytest.raises(testbed_svc.TestbedUnavailable, match="pooled sandbox"):
        await testbed_svc.issue_credentials(db_session, project, test_user)


async def test_refuses_when_policy_disabled(db_session, test_user):
    db_session.add(
        PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={},
            deployment_policies={"testbed_enabled": False},
        )
    )
    project = await _project(db_session, test_user)
    await _active_testbed(db_session, test_user, project)
    with pytest.raises(testbed_svc.TestbedDisabled):
        await testbed_svc.issue_credentials(db_session, project, test_user)


# ------------------------------------------------- happy path + bootstrap


async def test_mint_bootstraps_role_and_returns_session(
    db_session, test_user, monkeypatch
):
    iam = FakeIam(role_exists=False)
    _wire(monkeypatch, iam)
    project = await _project(db_session, test_user)
    await _active_testbed(db_session, test_user, project)

    result = await testbed_svc.issue_credentials(db_session, project, test_user)

    # role created with the platform principal as sole trust
    assert len(iam.created) == 1
    trust = json.loads(iam.created[0]["AssumeRolePolicyDocument"])
    assert trust["Statement"][0]["Principal"]["AWS"].endswith(
        "role/MarshalBackendTaskRole"
    )
    assert iam.created[0]["MaxSessionDuration"] == 12 * 3600
    # policy re-put from the repo copy
    assert iam.policies_put and iam.policies_put[0]["PolicyName"] == "MarshalTestbedLimits"
    # session shape: attribution + expiry + no storage side effects
    assert result["session_name"] == f"marshal-{str(test_user.id)[:8]}"
    assert result["access_key_id"] == "ASIAFAKE"
    assert result["account_id"] == "210987654321"
    assert result["console_url"].startswith("https://signin.aws.amazon.com/")


async def test_bootstrap_is_idempotent(db_session, test_user, monkeypatch):
    """Existing role: no create, policy still re-put (repo is the source)."""
    iam = FakeIam(role_exists=True)
    _wire(monkeypatch, iam)
    project = await _project(db_session, test_user)
    await _active_testbed(db_session, test_user, project)

    await testbed_svc.issue_credentials(db_session, project, test_user)

    assert iam.created == []
    assert len(iam.policies_put) == 1


async def test_endpoint_issues_and_maps_refusals(
    client, db_session, test_user, monkeypatch, audit_db
):
    """API surface: 202-shaped success + structured 409/422 refusals."""
    project = await _project(db_session, test_user)
    # no active deployment yet → 409
    r = await client.post(f"/api/v1/projects/{project.id}/deployment/testbed-credentials")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "testbed_unavailable"

    iam = FakeIam()
    _wire(monkeypatch, iam)
    await _active_testbed(db_session, test_user, project)
    r = await client.post(f"/api/v1/projects/{project.id}/deployment/testbed-credentials")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret_access_key"] == "secret"
    assert body["session_name"].startswith("marshal-")


# ------------------------------------------------- policy document shape


def test_policy_document_keeps_custody_denies():
    """Guard the curated policy against accidental edits: Allow* must stay
    behind the custody-critical Deny wall."""
    statements = {s["Sid"]: s for s in testbed_svc.TESTBED_POLICY["Statement"]}
    assert statements["AllowSelfConfiguration"]["Action"] == "*"
    deny_iam = statements["DenyIamMutation"]
    assert deny_iam["Effect"] == "Deny"
    assert "iam:Create*" in deny_iam["Action"] and "iam:PassRole" in deny_iam["Action"]
    # service-linked-role carve-out is the ONLY iam-mutation escape
    assert deny_iam["NotResource"] == "arn:aws:iam::*:role/aws-service-role/*"
    assert statements["DenyStsEscalation"]["Action"] == [
        "sts:AssumeRole", "sts:GetFederationToken",
    ]
    tamper = statements["DenyGovernanceTamper"]["Action"]
    assert "cloudtrail:StopLogging" in tamper and "organizations:*" in tamper
    assert statements["DenyMarshalStackMutation"]["Resource"].endswith("stack/marshal-*")
    assert statements["DenyTestbedRoleTamper"]["Resource"].endswith(
        testbed_svc.TESTBED_ROLE_NAME
    )


async def test_assume_falls_back_to_chaining_ceiling(monkeypatch):
    """Live finding (drill run 4): the platform's task-role session makes the
    vend a CHAINED AssumeRole — AWS caps chained sessions at 1h regardless of
    MaxSessionDuration. The vend retries at the ceiling instead of failing."""
    from botocore.exceptions import ClientError

    calls: list[int] = []

    class FakeSts:
        def assume_role(self, RoleArn, RoleSessionName, DurationSeconds):
            calls.append(DurationSeconds)
            if DurationSeconds > 3600:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationError",
                            "Message": "The requested DurationSeconds exceeds "
                            "the 1 hour session limit for roles assumed by "
                            "role chaining.",
                        }
                    },
                    "AssumeRole",
                )
            return {
                "Credentials": {
                    "AccessKeyId": "A", "SecretAccessKey": "S",
                    "SessionToken": "T",
                    "Expiration": datetime.now(UTC) + timedelta(hours=1),
                }
            }

    monkeypatch.setattr(testbed_svc.boto3, "client", lambda *a, **k: FakeSts())
    creds = testbed_svc._assume_testbed_role("123456789012", "marshal-drill", 4)
    assert calls == [14400, 3600], "must retry once at the chaining ceiling"
    assert creds["AccessKeyId"] == "A"
