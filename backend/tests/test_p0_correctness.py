"""P0 correctness wave: B23 anchoring/gating + B21 policy ordering.

Cross-cutting invariants live here; profile-specific auth matrices stay in
`test_deployed_app_auth` / `test_cdk_artifacts`.
"""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.models import CodegenBuild, Deployment, PlatformSettings, Project, Spec
from app.services import deployment as deploy_svc
from app.services import risk as risk_svc
from app.services.codegen import internal as internal_mod
from app.services.codegen.internal import InternalProvider
from app.services.codegen.literals import Contract, contracts_payload, extract_contracts
from app.services.codegen.provider import BuildCtx, PlannedFile
from app.services.policies import validate_policies

pytestmark = pytest.mark.asyncio


def test_negated_normative_contracts_are_not_extracted():
    assert extract_contracts(
        "- The API MUST NOT accept {ssn, date_of_birth}.\n"
        '- The result SHALL NOT return one of "secret", "credential".\n'
    ) == []
    positive = extract_contracts(
        '- status SHALL be one of "ready", "failed".\n'
        "- request SHALL accept exactly {reference, amount}."
    )
    assert [c.kind for c in positive] == ["enum_literals", "field_contract"]


async def test_literal_and_auth_contract_reach_every_prompt(monkeypatch):
    calls: list[dict] = []
    plan_json = json.dumps(
        {
            "app_name": "probe",
            "architecture_notes": "one api",
            "files": [
                {"path": "lib/app-stack.ts", "brief": "stack", "language": "typescript"},
                {"path": "src/api/index.py", "brief": "handler", "language": "python"},
                {"path": "README.md", "brief": "usage", "language": "markdown"},
            ],
        }
    )
    outputs = [
        plan_json,
        "export class AppStack {}",
        "# Probe",
        '{"Resources":{"Api":{"Type":"AWS::ApiGateway::RestApi"}},'
        '"Outputs":{"ApiUrl":{"Value":"x"}}}',
    ]

    async def fake_converse(**kwargs):
        calls.append(kwargs)
        return outputs[len(calls) - 1], {}, "end_turn"

    monkeypatch.setattr(internal_mod, "converse", fake_converse)
    literal = contracts_payload(
        [Contract("field_contract", ("applicant_ref", "annual_income"), "exactly")]
    )
    ctx = BuildCtx(
        build_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        project_name="Probe",
        user_id=None,
        spec_docs={"requirements": "# r", "design": "# d", "tasks": "# t"},
        model_id="m",
        max_tokens=8192,
        endpoint_auth={"mode": "key_required", "source": "default"},
        require_api_key=True,
        literal_contract=literal,
    )
    provider = InternalProvider()
    plan = await provider.plan(ctx, profile="cdk-app")
    ts = next(f for f in plan.files if f.language == "typescript")
    await provider.generate_file(ctx, plan, ts, {})
    readme = await provider.generate_file(
        ctx, plan, PlannedFile("README.md", "usage", "markdown"), {}
    )
    await provider.assemble_template(ctx, plan, {"src/api/index.py": "def handler(e,c): pass"})

    prompts = [c["messages"][0]["content"][0]["text"] for c in calls]
    assert all("applicant_ref" in p and "annual_income" in p for p in prompts)
    assert "apiKeyRequired" in prompts[1]  # CDK TypeScript contract
    assert "x-api-key" in readme and "Reveal key" in readme


async def test_always_policy_refuses_public_before_risk_or_deployment(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    settings = PlatformSettings(
        id=1,
        model_allowlist=["m"],
        param_bounds={},
        rate_limits={},
        cost={},
        deployment_policies={"require_endpoint_auth": "always"},
    )
    db_session.add(settings)
    project = Project(user_id=test_user.id, name="Strict auth", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="ready",
        created_by=test_user.id,
        manifest={
            "endpoint_auth": {"mode": "open", "source": "explicit_public"}
        },
        spec_snapshot={
            "requirements": {
                "version": 1,
                "content": "Endpoint authentication: PUBLIC",
            }
        },
    )
    db_session.add(build)
    await db_session.commit()

    calls: list[str] = []

    async def risk_gate(*args, **kwargs):
        calls.append("risk")

    async def start(*args, **kwargs):
        calls.append("deploy")
        raise AssertionError("must not start")

    monkeypatch.setattr(risk_svc, "deployment_gate", risk_gate)
    monkeypatch.setattr(deploy_svc, "start_deployment", start)

    async with client_for(test_user) as client:
        response = await client.post(
            f"/api/v1/projects/{project.id}/deploy", json={"build_id": str(build.id)}
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "policy_endpoint_auth"
    assert calls == []
    count = (
        await db_session.execute(
            select(func.count()).select_from(Deployment).where(
                Deployment.project_id == project.id
            )
        )
    ).scalar_one()
    assert count == 0


async def test_key_expected_but_unreadable_smoke_is_inconclusive(
    db_session, test_user, monkeypatch
):
    project = Project(user_id=test_user.id, name="Missing key", status="deployed")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="ready",
        created_by=test_user.id,
        manifest={},
    )
    db_session.add(build)
    await db_session.flush()
    deployment = Deployment(
        project_id=project.id,
        user_id=test_user.id,
        build_id=build.id,
        status="active",
        health="healthy",
        app_url="https://example/prod",
        timeline=[],
        resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()

    class NeverHttp:
        def __init__(self, *args, **kwargs):
            raise AssertionError("expected-key smoke must not run keyless HTTP")

    monkeypatch.setattr(deploy_svc.httpx, "AsyncClient", NeverHttp)
    await deploy_svc._post_deploy_smoke(
        db_session,
        deployment,
        "{}",
        api_key=None,
        api_key_expected=True,
    )
    await db_session.refresh(build)
    await db_session.refresh(deployment)
    assert deployment.health == "healthy"
    assert build.manifest["smoke"]["passed"] is None
    assert build.manifest["smoke"]["reason"] == "api_key_value_unavailable"


def test_endpoint_auth_policy_validation():
    validate_policies({"require_endpoint_auth": "default"})
    validate_policies({"require_endpoint_auth": "always"})
    with pytest.raises(ValueError, match="require_endpoint_auth"):
        validate_policies({"require_endpoint_auth": "sometimes"})


async def test_risk_payload_exposes_auth_intent_before_scoring(
    db_session, test_user, client_for, audit_db
):
    project = Project(user_id=test_user.id, name="Public intent", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        Spec(
            project_id=project.id,
            version=1,
            type="requirements",
            content="Endpoint authentication: PUBLIC",
            created_by=test_user.id,
        )
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        response = await client.get(f"/api/v1/projects/{project.id}/risk")
    body = response.json()
    assert body["assessed"] is False
    assert body["endpoint_auth"] == "open"
    assert body["endpoint_auth_source"] == "explicit_public"
