"""B13 deployed-app auth (integration-wave spec R1): signal detection, the
auth_contract gate, keyed smoke, and the owner reveal path."""

import json
import uuid

import pytest

from app.models import Deployment, Lease, Project, Spec
from app.services import deployment as deploy_svc
from app.services.codegen.validate import (
    Finding,
    endpoint_auth_decision,
    requires_api_key,
    validate_artifacts,
)

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------- signal (R1.1)


def test_signal_detection_is_narrow():
    assert endpoint_auth_decision("") == {"mode": "key_required", "source": "default"}
    assert requires_api_key("All endpoints SHALL require an API key.")
    assert endpoint_auth_decision("api key required")["source"] == "explicit_key"
    assert endpoint_auth_decision("Endpoint authentication: PUBLIC") == {
        "mode": "open", "source": "explicit_public",
    }
    assert endpoint_auth_decision(
        "All endpoints SHALL be publicly accessible without authentication."
    )["mode"] == "open"
    # Casual prose and negated key prose do NOT opt out: safe default remains.
    for text in (
        "We may add an API key in a later phase.",
        "The service uses IAM auth, not API keys.",
        "This is a public-facing demo.",
        "The API may be open someday.",
        "The API does not require an API key.",
        "keyed access to the data API",
    ):
        assert endpoint_auth_decision(text) == {
            "mode": "key_required", "source": "default",
        }
    conflict = endpoint_auth_decision(
        "All endpoints SHALL require an API key.\nEndpoint authentication: PUBLIC"
    )
    assert conflict == {"mode": "key_required", "source": "conflict"}


# ------------------------------------------------- auth_contract (R1.2)


def _keyed_template(*, with_key=True, with_plan_key=True, protected=True, with_output=True):
    resources = {
        "Fn": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Code": {"ZipFile": "def handler(event, context):\n    return {'statusCode': 200}"},
                "Handler": "index.handler", "Runtime": "python3.12",
                "Role": {"Fn::GetAtt": ["Role", "Arn"]},
            },
        },
        "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
        "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
        "GetMethod": {
            "Type": "AWS::ApiGateway::Method",
            "Properties": {"HttpMethod": "GET", "ApiKeyRequired": protected},
        },
        "Cors": {
            "Type": "AWS::ApiGateway::Method",
            "Properties": {"HttpMethod": "OPTIONS"},  # never key-gated
        },
    }
    if with_key:
        resources["Key"] = {"Type": "AWS::ApiGateway::ApiKey", "Properties": {"Enabled": True}}
        resources["Plan"] = {
            "Type": "AWS::ApiGateway::UsagePlan",
            # ApiStages is REQUIRED: a stage-less plan accepts the key for no
            # stage and every request 403s (live drill finding, 24 Aug 2026)
            "Properties": {"ApiStages": [{"ApiId": {"Ref": "Api"}, "Stage": "prod"}]},
        }
        if with_plan_key:
            resources["PlanKey"] = {"Type": "AWS::ApiGateway::UsagePlanKey", "Properties": {}}
    outputs = {"ApiUrl": {"Value": "https://example"}}
    if with_output:
        # G25: the valid CloudFormation shape — a bare {"Ref": …} body is
        # what CloudFormation rejects at CreateStack
        outputs["ApiKeyId"] = {"Value": {"Ref": "Key"}}
    return json.dumps({"Resources": resources, "Outputs": outputs})


def _artifacts(template: str) -> dict[str, str]:
    return {
        "template.json": template,
        "src/handler.py": "def handler(event, context):\n    return {'statusCode': 200}\n",
        "README.md": "# x",
    }


def test_auth_contract_passes_conforming_template():
    findings, _ = validate_artifacts(
        _artifacts(_keyed_template()), [], require_api_key=True
    )
    assert findings == []


def test_auth_contract_findings_matrix():
    def auth_findings(template) -> list[Finding]:
        findings, _ = validate_artifacts(_artifacts(template), [], require_api_key=True)
        return [f for f in findings if f.check == "auth_contract"]

    missing_key = auth_findings(_keyed_template(with_key=False, with_output=False))
    assert any("ApiKey resource" in f.message for f in missing_key)
    assert any("Outputs.ApiKeyId" in f.message for f in missing_key)

    unprotected = auth_findings(_keyed_template(protected=False))
    assert any("ApiKeyRequired" in f.message and "GetMethod" in f.message for f in unprotected)

    no_plan_key = auth_findings(_keyed_template(with_plan_key=False))
    assert any("UsagePlanKey" in f.message for f in no_plan_key)

    # the live-drill class: plan present but bound to NO stage → keyed 403s
    template = json.loads(_keyed_template())
    template["Resources"]["Plan"]["Properties"] = {}
    stage_less = auth_findings(json.dumps(template))
    assert any("ApiStages" in f.message for f in stage_less)


def test_default_keyed_and_explicit_public_gate_shapes():
    open_artifacts = _artifacts(
        _keyed_template(with_key=False, protected=False, with_output=False)
    )

    default_findings, _ = validate_artifacts(
        open_artifacts,
        [],
        endpoint_auth=endpoint_auth_decision("# no auth statement"),
    )
    assert any(f.check == "auth_contract" for f in default_findings)

    public_findings, _ = validate_artifacts(
        open_artifacts,
        [],
        endpoint_auth=endpoint_auth_decision("Endpoint authentication: PUBLIC"),
    )
    assert [f for f in public_findings if "auth_contract" in f.check] == []

    keyed_under_public, _ = validate_artifacts(
        _artifacts(_keyed_template()),
        [],
        endpoint_auth=endpoint_auth_decision("Endpoint authentication: PUBLIC"),
    )
    assert any(f.check == "public_auth_contract" for f in keyed_under_public)

    conflict, _ = validate_artifacts(
        _artifacts(_keyed_template()),
        [],
        endpoint_auth=endpoint_auth_decision(
            "All endpoints SHALL require an API key.\nEndpoint authentication: PUBLIC"
        ),
    )
    assert any(f.check == "endpoint_auth_signal" for f in conflict)


# ------------------------------------------------- keyed smoke (R1.3)


async def test_smoke_sends_key_header(db_session, test_user, monkeypatch):
    project = Project(user_id=test_user.id, name="Keyed", status="deployed")
    db_session.add(project)
    await db_session.flush()
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active",
        app_url="https://keyed.example/prod",
    )
    db_session.add(deployment)
    await db_session.commit()

    seen_headers: list[dict] = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **kwargs):
            seen_headers.append(dict(kwargs.get("headers") or {}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(deploy_svc.httpx, "AsyncClient", FakeClient)
    template = _keyed_template()
    await deploy_svc._post_deploy_smoke(db_session, deployment, template, api_key="k-123")
    assert seen_headers and seen_headers[0].get("x-api-key") == "k-123"
    # keyless call keeps the pre-B13 shape
    await deploy_svc._post_deploy_smoke(db_session, deployment, template, api_key=None)
    assert seen_headers[-1] == {}


async def test_fetch_api_key_reads_output(monkeypatch):
    calls = {}

    class FakeApigw:
        def get_api_key(self, apiKey, includeValue):  # noqa: N803 — boto casing
            calls["id"] = apiKey
            calls["include"] = includeValue
            return {"value": "secret-value"}

    class FakeSession:
        def client(self, name):
            assert name == "apigateway"
            return FakeApigw()

    value = await deploy_svc._fetch_api_key(FakeSession(), {"ApiKeyId": "abc123"})
    assert value == "secret-value" and calls == {"id": "abc123", "include": True}
    assert await deploy_svc._fetch_api_key(FakeSession(), {}) is None


# ------------------------------------------------- owner reveal (R1.4)


async def test_reveal_endpoint(db_session, test_user, client_for, audit_db, monkeypatch):
    project = Project(user_id=test_user.id, name="Revealable", status="deployed")
    db_session.add(project)
    await db_session.flush()
    lease = Lease(provider="direct", project_id=project.id, user_id=test_user.id, status="active")
    db_session.add(lease)
    await db_session.flush()
    db_session.add(
        Deployment(
            project_id=project.id, user_id=test_user.id, status="active",
            stack_name="marshal-x", lease_id=lease.id,
        )
    )
    await db_session.commit()

    async def fake_outputs(cfn, stack_name):
        return {"ApiKeyId": "key-1", "ApiUrl": "https://x"}

    async def fake_fetch(session, outputs):
        return "the-value"

    class FakeProvider:
        def deployment_session(self, lease_info):
            class S:
                def client(self, name):
                    return object()
            return S()

    monkeypatch.setattr(deploy_svc, "_stack_outputs", fake_outputs)
    monkeypatch.setattr(deploy_svc, "_fetch_api_key", fake_fetch)
    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: FakeProvider())

    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/deployment/api-key/reveal")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["value"] == "the-value" and body["api_key_id"] == "key-1"


async def test_reveal_404_when_keyless(db_session, test_user, client_for, audit_db, monkeypatch):
    project = Project(user_id=test_user.id, name="Open", status="deployed")
    db_session.add(project)
    await db_session.flush()
    lease = Lease(provider="direct", project_id=project.id, user_id=test_user.id, status="active")
    db_session.add(lease)
    await db_session.flush()
    db_session.add(
        Deployment(
            project_id=project.id, user_id=test_user.id, status="active",
            stack_name="marshal-y", lease_id=lease.id,
        )
    )
    await db_session.commit()

    async def fake_outputs(cfn, stack_name):
        return {"ApiUrl": "https://x"}  # no ApiKeyId

    class FakeProvider:
        def deployment_session(self, lease_info):
            class S:
                def client(self, name):
                    return object()
            return S()

    monkeypatch.setattr(deploy_svc, "_stack_outputs", fake_outputs)
    monkeypatch.setattr(deploy_svc, "get_sandbox_provider", lambda: FakeProvider())

    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/deployment/api-key/reveal")
        assert r.status_code == 404
        # no active deployment at all → 409
        r2 = await client.post(
            f"/api/v1/projects/{uuid.uuid4()}/deployment/api-key/reveal"
        )
        assert r2.status_code == 404  # invisible project stays a 404


# ------------------------------------------------- risk display (R1.5)


async def test_risk_route_displays_endpoint_auth(db_session, test_user, client_for, audit_db):
    project = Project(user_id=test_user.id, name="Signal", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        Spec(
            project_id=project.id, version=1, type="requirements",
            content="1. Every endpoint SHALL require an API key.",
            created_by=test_user.id,
        )
    )
    from app.models import AiRiskAssessment

    db_session.add(
        AiRiskAssessment(
            project_id=project.id, content_hash="h" * 64, rubric_version=1,
            score=20, level="low", factors={}, status="scored", decision="auto_approved",
        )
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        r = await client.get(f"/api/v1/projects/{project.id}/risk")
        assert r.status_code == 200
        assert r.json()["endpoint_auth"] == "key_required"
        assert r.json()["endpoint_auth_source"] == "explicit_key"


def test_console_exemption_requires_the_b19_signal():
    doc = json.loads(_keyed_template(protected=False))
    doc["Resources"]["GetMethod"]["Properties"]["Integration"] = {
        "Uri": {"Fn::GetAtt": ["WebConsoleFunction", "Arn"]}
    }
    artifacts = _artifacts(json.dumps(doc))
    keyed = {"mode": "key_required", "source": "default"}

    findings, _ = validate_artifacts(artifacts, [], endpoint_auth=keyed)
    assert any(f.check == "auth_contract" and "GetMethod" in f.message for f in findings)

    with_signal, _ = validate_artifacts(
        artifacts, [], endpoint_auth=keyed, require_web_console=True
    )
    assert not any(
        f.check == "auth_contract" and "GetMethod" in f.message for f in with_signal
    )
