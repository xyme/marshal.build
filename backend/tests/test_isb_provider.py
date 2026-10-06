"""IsbHttpProvider contract tests — real ISB API shape (JSend + service JWT)."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt as pyjwt
import pytest

from app.services.sandbox import direct as direct_mod
from app.services.sandbox.base import LeaseInfo
from app.services.sandbox.direct import DirectAccountProvider, deploy_session_name
from app.services.sandbox.isb import IsbApiError, IsbHttpProvider

SECRET = "unit-test-secret"


def make_provider(handler) -> IsbHttpProvider:
    transport = httpx.MockTransport(handler)
    provider = IsbHttpProvider(
        base_url="https://isb.example.com/prod", transport=transport, jwt_secret=SECRET
    )
    provider.activation_timeout_s = 3
    return provider


def jsend(data, status_code=200) -> httpx.Response:
    return httpx.Response(status_code, json={"status": "success", "data": data})


async def test_bearer_token_is_valid_service_jwt():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return jsend({"result": []})

    provider = make_provider(handler)
    await provider._request("GET", "/leaseTemplates")

    assert seen["auth"].startswith("Bearer ")
    claims = pyjwt.decode(seen["auth"].split(" ", 1)[1], SECRET, algorithms=["HS256"])
    assert claims["user"]["roles"] == ["Admin"]
    assert claims["user"]["email"]
    assert "exp" in claims


LEASE_UUID = "1cb85f3f-3c8f-44f9-a34a-04549e29c9f5"
LEASE_EMAIL = "orchestrator@marshal.ai"


def composite_id(email: str = LEASE_EMAIL, uuid: str = LEASE_UUID) -> str:
    import base64

    return base64.b64encode(
        json.dumps({"userEmail": email, "uuid": uuid}, separators=(",", ":")).encode()
    ).decode()


async def test_request_lease_creates_template_then_lease_and_polls_to_active():
    calls = []
    lease_states = iter(["Provisioning", "Active"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/prod/leaseTemplates":
            return jsend({"result": []})
        if request.method == "POST" and request.url.path == "/prod/leaseTemplates":
            body = json.loads(request.content)
            assert body["requiresApproval"] is False
            assert body["maxSpend"] == 1000
            assert body["leaseDurationInHours"] == 720
            return jsend({"uuid": "tmpl-1", "name": body["name"]}, 201)
        if request.method == "POST" and request.url.path == "/prod/leases":
            body = json.loads(request.content)
            assert body["leaseTemplateUuid"] == "tmpl-1"
            return jsend(
                {"uuid": LEASE_UUID, "userEmail": LEASE_EMAIL, "status": "PendingApproval"}, 201
            )
        # Polling must use the base64 composite key (userEmail + uuid), not the raw uuid
        if request.method == "GET" and request.url.path == f"/prod/leases/{composite_id()}":
            return jsend(
                {
                    "uuid": LEASE_UUID,
                    "userEmail": LEASE_EMAIL,
                    "status": next(lease_states),
                    "awsAccountId": "123456789012",
                }
            )
        raise AssertionError(f"unexpected call {request.method} {request.url.path}")

    provider = make_provider(handler)
    provider_module_sleep_patch = __import__("app.services.sandbox.isb", fromlist=["ACTIVATION_POLL_SECONDS"])
    provider_module_sleep_patch.ACTIVATION_POLL_SECONDS = 0

    lease = await provider.request_lease(project_id="p1", user_id="u1")
    assert lease.status == "active"
    assert lease.aws_account_id == "123456789012"
    assert lease.external_lease_id == composite_id()


async def test_lease_template_reused_when_exists():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/prod/leaseTemplates":
            return jsend({"result": [{"uuid": "tmpl-9", "name": "marshal-default"}]})
        if request.url.path == "/prod/leases":
            return jsend(
                {"uuid": LEASE_UUID, "userEmail": LEASE_EMAIL, "status": "Active", "awsAccountId": "1"},
                201,
            )
        raise AssertionError("unexpected")

    provider = make_provider(handler)
    lease = await provider.request_lease(project_id="p", user_id="u")
    assert lease.status == "active"
    assert ("POST", "/prod/leaseTemplates") not in calls


async def test_terminate_lease_posts_terminate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return jsend({})

    provider = make_provider(handler)
    await provider.terminate_lease("lease-7")
    assert calls == [("POST", "/prod/leases/lease-7/terminate")]


async def test_status_mapping():
    for isb_status, expected in [
        ("Active", "active"),
        ("Provisioning", "requested"),
        ("PendingApproval", "requested"),
        ("Frozen", "failed"),
        ("ManuallyTerminated", "terminated"),
        ("BudgetExceeded", "terminated"),
        ("AccountQuarantined", "failed"),
    ]:
        assert IsbHttpProvider._to_lease({"status": isb_status}).status == expected


async def test_api_error_surfaces_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"status": "fail", "data": {"errors": [{"message": "max leases"}]}})

    provider = make_provider(handler)
    with pytest.raises(IsbApiError, match="409"):
        await provider._request("POST", "/leases", {})


async def test_requires_base_url():
    from unittest.mock import patch

    with patch("app.services.sandbox.isb.get_settings") as settings:
        settings.return_value.isb_api_base_url = ""
        settings.return_value.aws_region = "us-east-1"
        with pytest.raises(ValueError, match="ISB_API_BASE_URL"):
            IsbHttpProvider()


# ============================================================ DirectAccountProvider
# B6: in the cloud the direct provider must NOT deploy on the task role — it
# assumes the scoped MarshalDirectDeployRole (DIRECT_DEPLOY_ROLE_ARN) once per
# deployment (RoleSessionName = marshal-deploy-<deployment id>) and reuses the
# credentials for that deployment; unset → ambient-credential fallback.

DEPLOY_ROLE_ARN = "arn:aws:iam::123456789012:role/MarshalDirectDeployRole"


def _direct_settings(monkeypatch, role_arn: str):
    monkeypatch.setattr(
        direct_mod,
        "get_settings",
        lambda: SimpleNamespace(aws_region="us-east-1", direct_deploy_role_arn=role_arn),
    )


def _stub_sts(monkeypatch, calls: list[dict], *, ttl_s: int = 3600):
    class FakeSts:
        def assume_role(self, **kwargs):
            calls.append(kwargs)
            n = len(calls)
            return {
                "Credentials": {
                    "AccessKeyId": f"ASIAFAKE{n}",
                    "SecretAccessKey": f"secret-{n}",
                    "SessionToken": f"token-{n}",
                    "Expiration": datetime.now(UTC) + timedelta(seconds=ttl_s),
                }
            }

    def fake_client(service, **kwargs):
        assert service == "sts"
        return FakeSts()

    monkeypatch.setattr(direct_mod.boto3, "client", fake_client)


def test_direct_assumes_deploy_role_when_configured(monkeypatch):
    _direct_settings(monkeypatch, DEPLOY_ROLE_ARN)
    calls: list[dict] = []
    _stub_sts(monkeypatch, calls)
    provider = DirectAccountProvider()
    lease = LeaseInfo(
        external_lease_id="direct-abc123def456",
        aws_account_id="123456789012",
        status="active",
        deployment_id="0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    )

    session = provider.deployment_session(lease)

    assert len(calls) == 1
    assert calls[0]["RoleArn"] == DEPLOY_ROLE_ARN
    assert calls[0]["RoleSessionName"] == "marshal-deploy-0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert calls[0]["DurationSeconds"] == 3600
    creds = session.get_credentials().get_frozen_credentials()
    assert (creds.access_key, creds.secret_key, creds.token) == ("ASIAFAKE1", "secret-1", "token-1")
    assert session.region_name == "us-east-1"


def test_direct_caches_credentials_per_deployment(monkeypatch):
    _direct_settings(monkeypatch, DEPLOY_ROLE_ARN)
    calls: list[dict] = []
    _stub_sts(monkeypatch, calls)
    provider = DirectAccountProvider()
    dep_a = LeaseInfo("direct-a", "123456789012", "active", deployment_id="dep-a")
    dep_b = LeaseInfo("direct-b", "123456789012", "active", deployment_id="dep-b")

    first = provider.deployment_session(dep_a)
    again = provider.deployment_session(dep_a)  # deploy → poll → teardown reuse
    other = provider.deployment_session(dep_b)

    assert len(calls) == 2  # one assume per deployment, not per call
    assert [c["RoleSessionName"] for c in calls] == ["marshal-deploy-dep-a", "marshal-deploy-dep-b"]
    assert first.get_credentials().access_key == again.get_credentials().access_key
    assert other.get_credentials().access_key != first.get_credentials().access_key
    # Both sessions of one deployment share ONE credential object, so a
    # refresh triggered by either is seen by every client built from them.
    assert first.get_credentials() is again.get_credentials()


def test_direct_session_credentials_refresh_before_expiry(monkeypatch):
    """A client held across an unbounded stack poll must not die with
    ExpiredToken: the session's credentials re-assume the role on demand
    (botocore refreshes when < 15 min remain). Expired cache entries are
    pruned when the next deployment's credentials are inserted."""
    _direct_settings(monkeypatch, DEPLOY_ROLE_ARN)
    calls: list[dict] = []
    _stub_sts(monkeypatch, calls, ttl_s=120)  # inside botocore's refresh window
    provider = DirectAccountProvider()
    dep_a = LeaseInfo("direct-a", "123456789012", "active", deployment_id="dep-a")

    session = provider.deployment_session(dep_a)
    assert len(calls) == 1
    creds = session.get_credentials()  # what every client built from it uses
    frozen = creds.get_frozen_credentials()  # the long poll asks again later…
    assert len(calls) == 2  # …and the credentials re-assumed instead of expiring
    assert frozen.access_key == "ASIAFAKE2"
    assert calls[1]["RoleSessionName"] == "marshal-deploy-dep-a"  # same attribution
    assert [c["RoleArn"] for c in calls] == [DEPLOY_ROLE_ARN, DEPLOY_ROLE_ARN]

    # Eviction (finding 5): an entry whose credentials have lapsed is dropped
    # when another deployment's credentials are inserted.
    creds._expiry_time = datetime.now(UTC) - timedelta(seconds=1)  # dep-a lapsed
    provider.deployment_session(LeaseInfo("direct-b", "123456789012", "active", deployment_id="dep-b"))
    assert set(provider._credentials) == {"dep-b"}
    # A later call for dep-a simply re-assumes (cache miss), same as a cold start.
    provider.deployment_session(dep_a)
    assert set(provider._credentials) == {"dep-a", "dep-b"}
    assert calls[-1]["RoleSessionName"] == "marshal-deploy-dep-a"


def test_isb_deployment_session_refreshes_blueprint_role(monkeypatch):
    """The ISB provider's deployment session has the same refresh property —
    the pre-existing 1 h static-credential edge on long creates is gone."""
    from app.services.sandbox import isb as isb_mod

    calls: list[dict] = []

    class FakeSts:
        def assume_role(self, **kwargs):
            calls.append(kwargs)
            n = len(calls)
            return {
                "Credentials": {
                    "AccessKeyId": f"ASIAISB{n}",
                    "SecretAccessKey": f"isb-secret-{n}",
                    "SessionToken": f"isb-token-{n}",
                    "Expiration": datetime.now(UTC) + timedelta(seconds=120),
                }
            }

    monkeypatch.setattr(isb_mod.boto3, "client", lambda service, **kw: FakeSts())
    provider = make_provider(lambda request: jsend({"result": []}))
    provider.deployment_role_name = "AIFactoryDeploymentRole"
    lease = LeaseInfo("lease-1", "123456789012", "active", deployment_id="dep-isb")

    session = provider.deployment_session(lease)
    assert len(calls) == 1
    assert calls[0] == {
        "RoleArn": "arn:aws:iam::123456789012:role/AIFactoryDeploymentRole",
        "RoleSessionName": "marshal-deploy",
    }
    assert session.region_name == provider.region
    frozen = session.get_credentials().get_frozen_credentials()
    assert len(calls) == 2  # near expiry → re-assumed on demand
    assert (frozen.access_key, frozen.secret_key, frozen.token) == (
        "ASIAISB2", "isb-secret-2", "isb-token-2",
    )


def test_direct_falls_back_to_ambient_credentials_when_unset(monkeypatch, caplog):
    import logging

    _direct_settings(monkeypatch, "")
    calls: list[dict] = []
    _stub_sts(monkeypatch, calls)
    provider = DirectAccountProvider()
    lease = LeaseInfo("direct-x", "123456789012", "active", deployment_id="dep-x")

    with caplog.at_level(logging.WARNING, logger="marshal.sandbox.direct"):
        session = provider.deployment_session(lease)
        provider.deployment_session(lease)

    assert calls == []  # no STS call at all
    assert session.region_name == "us-east-1"
    warnings = [r for r in caplog.records if "DIRECT_DEPLOY_ROLE_ARN unset" in r.getMessage()]
    assert len(warnings) == 1  # one-line warning, once per provider


def test_direct_session_name_shape(monkeypatch):
    # Attribution key: deployment id, else the lease id, else a fixed marker.
    assert deploy_session_name("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0") == (
        "marshal-deploy-0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    )
    assert deploy_session_name(None) == "marshal-deploy-adhoc"
    # STS allows [\w+=,.@-] and at most 64 chars.
    long_name = deploy_session_name("x" * 100)
    assert len(long_name) == 64 and long_name.startswith("marshal-deploy-")
    assert deploy_session_name("a/b c:d") == "marshal-deploy-a-b-c-d"

    # The provider prefers the deployment id and falls back to the lease id.
    _direct_settings(monkeypatch, DEPLOY_ROLE_ARN)
    calls: list[dict] = []
    _stub_sts(monkeypatch, calls)
    provider = DirectAccountProvider()
    provider.deployment_session(
        LeaseInfo("direct-abc", "123456789012", "active", deployment_id="dep-1")
    )
    provider.deployment_session(LeaseInfo("direct-abc", "123456789012", "active"))
    assert [c["RoleSessionName"] for c in calls] == [
        "marshal-deploy-dep-1",
        "marshal-deploy-direct-abc",
    ]
