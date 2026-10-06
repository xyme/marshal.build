"""Codegen build pipeline tests (codegen-handoff spec)."""

import asyncio
import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import select

from app.models import CodegenArtifact, CodegenBuild, Deployment, Project, Spec, User
from app.services.codegen import provider as provider_mod
from app.services.codegen import runner
from app.services.codegen.provider import BuildCtx, BuildPlan, PlannedFile
from app.services.codegen.validate import validate_artifacts

pytestmark = pytest.mark.asyncio

GOOD_TEMPLATE = json.dumps(
    {
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {"ZipFile": "def handler(event, context):\n    return {'statusCode': 200}"},
                    "Handler": "index.handler",
                    "Runtime": "python3.12",
                    "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                },
            },
            "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
        },
        "Outputs": {"ApiUrl": {"Value": "https://example"}},
    }
)
GOOD_ARTIFACTS = {
    "template.json": GOOD_TEMPLATE,
    "src/handler.py": "def handler(event, context):\n    return {'statusCode': 200}\n",
    "README.md": "# Demo\n",
}


class FakeProvider:
    """Deterministic provider for pipeline tests (interface conformance)."""

    name = "internal"

    def __init__(self, *, fail_file: str | None = None, template: str = GOOD_TEMPLATE):
        self.fail_file = fail_file
        self.template = template
        self.calls: list[str] = []
        self.planned_profiles: list[str] = []  # 13.5Z: what the runner told us

    async def plan(self, ctx: BuildCtx, *, profile: str = "inline-cfn") -> BuildPlan:
        self.calls.append("plan")
        self.planned_profiles.append(profile)
        return BuildPlan(
            app_name="probe-app",
            architecture_notes="One lambda behind a REST API.",
            files=[
                PlannedFile(path="src/handler.py", brief="the handler"),
                PlannedFile(path="README.md", brief="docs", language="markdown"),
            ],
        )

    async def generate_file(self, ctx, plan, file, generated):
        self.calls.append(file.path)
        if file.path == self.fail_file:
            raise ValueError("boom")
        return GOOD_ARTIFACTS[file.path]

    async def assemble_template(self, ctx, plan, generated):
        self.calls.append("template.json")
        return self.template


async def _project_with_docs(db, owner: User) -> Project:
    project = Project(user_id=owner.id, name="Buildable", status="spec_complete")
    db.add(project)
    await db.flush()
    for i, doc_type in enumerate(("requirements", "design", "tasks")):
        db.add(
            Spec(
                project_id=project.id, version=1, type=doc_type,
                content=(
                    f"# {doc_type}\n\n## Section\ncontent {i}\n\n"
                    + ("Endpoint authentication: PUBLIC\n" if doc_type == "requirements" else "")
                ),
                created_by=owner.id,
            )
        )
    await db.commit()
    await db.refresh(project)
    return project


async def _wait_terminal(db, build_id, timeout=5.0) -> CodegenBuild:
    for _ in range(int(timeout * 20)):
        await asyncio.sleep(0.05)
        build = await db.get(CodegenBuild, build_id)
        await db.refresh(build)
        if build.status in runner.TERMINAL_STATES:
            return build
    raise AssertionError("build did not reach a terminal state")


# ------------------------------------------------------------- validation gate


def test_validator_green_fixture():
    findings, unenforceable = validate_artifacts(dict(GOOD_ARTIFACTS), ["hardcoded_credentials"])
    assert findings == [] and unenforceable == []


def test_validator_matrix():
    # bad JSON
    bad = dict(GOOD_ARTIFACTS, **{"template.json": "not json"})
    findings, _ = validate_artifacts(bad, [])
    assert any(f.check == "template_json" for f in findings)

    # disallowed resource type
    doc = json.loads(GOOD_TEMPLATE)
    doc["Resources"]["Bad"] = {"Type": "AWS::EC2::Instance", "Properties": {}}
    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS, **{"template.json": json.dumps(doc)}), [])
    assert any(f.check == "service_allowlist" for f in findings)

    # non-inline code + bad runtime
    doc = json.loads(GOOD_TEMPLATE)
    doc["Resources"]["Fn"]["Properties"]["Code"] = {"S3Bucket": "b", "S3Key": "k"}
    doc["Resources"]["Fn"]["Properties"]["Runtime"] = "nodejs20.x"
    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS, **{"template.json": json.dumps(doc)}), [])
    checks = {f.check for f in findings}
    assert "inline_packaging" in checks

    # missing ApiUrl output
    doc = json.loads(GOOD_TEMPLATE)
    doc["Outputs"] = {}
    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS, **{"template.json": json.dumps(doc)}), [])
    assert any(f.check == "deploy_contract" and "ApiUrl" in f.message for f in findings)

    # G25: an output whose body is the bare intrinsic (no "Value") passed the
    # gate and CloudFormation then refused CreateStack ("Invalid outputs
    # property : [Fn::Sub]") — observed live, evaluate-tier proof 4 Oct 2026
    doc = json.loads(GOOD_TEMPLATE)
    doc["Outputs"] = {
        "ApiUrl": {"Fn::Sub": "https://${Api}.execute-api.${AWS::Region}.amazonaws.com/prod"},
        "ApiKeyId": {"Ref": "Key"},
    }
    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS, **{"template.json": json.dumps(doc)}), [])
    bad = [f for f in findings if f.check == "deploy_contract" and '"Value"' in f.message]
    assert sorted(f.message.split()[0] for f in bad) == ["Outputs.ApiKeyId", "Outputs.ApiUrl"]
    # the well-formed shape is accepted
    doc["Outputs"] = {k: {"Value": v} for k, v in doc["Outputs"].items()}
    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS, **{"template.json": json.dumps(doc)}), [])
    assert not [f for f in findings if f.check == "deploy_contract"]

    # hardcoded credential caught by the named guardrail pattern
    leaky = dict(GOOD_ARTIFACTS, **{"src/handler.py": 'API_KEY = "sk-live-abcdef123456789"\n'})  # gitleaks:allow — synthetic fixture
    findings, _ = validate_artifacts(leaky, ["hardcoded_credentials"])
    assert any(f.check == "guardrail_patterns" for f in findings)

    # unknown pattern names are reported, not silently dropped
    _, unenforceable = validate_artifacts(dict(GOOD_ARTIFACTS), ["quantum_entanglement"])
    assert unenforceable == ["quantum_entanglement"]


# ------------------------------------------------------------- provider seam


async def test_provider_selection(monkeypatch):
    # Construction by name (S9): internal always works; kiro needs the bucket
    assert provider_mod.get_provider("internal").name == "internal"
    monkeypatch.delenv("CODEGEN_WORKSPACE_BUCKET", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(provider_mod.CodegenUnavailable):
        provider_mod.get_provider("kiro")  # workspace bucket unconfigured
    with pytest.raises(provider_mod.CodegenUnavailable):
        provider_mod.get_provider("wat")

    # Resolution: platform setting wins over env; env is the bootstrap default
    monkeypatch.setenv("CODEGEN_PROVIDER", "internal")
    assert await provider_mod.resolve_provider_name() == "internal"
    monkeypatch.setenv("CODEGEN_PROVIDER", "garbage")
    assert await provider_mod.resolve_provider_name() == "internal"  # fail-safe


async def test_provider_alias_kiro_is_runner(monkeypatch):
    """`kiro` is a legacy alias for `runner` at every entry point."""
    from app.core.config import get_settings
    from app.services.codegen.workspace_runner import WorkspaceRunnerProvider

    assert provider_mod.normalize_provider_name("kiro") == "runner"
    assert provider_mod.normalize_provider_name(" KIRO ") == "runner"
    assert provider_mod.normalize_provider_name("runner") == "runner"
    assert provider_mod.normalize_provider_name("internal") == "internal"
    assert provider_mod.normalize_provider_name(None) == "internal"
    assert provider_mod.normalize_provider_name("wat") == "wat"  # unknown stays unknown
    assert provider_mod.PROVIDER_NAMES == ("internal", "runner")

    # With a bucket configured the alias constructs the runner provider
    # (constructor reads settings only — no boto3 client is built).
    monkeypatch.setenv("CODEGEN_WORKSPACE_BUCKET", "test-bucket")
    get_settings.cache_clear()
    try:
        provider = provider_mod.get_provider("kiro")
        assert isinstance(provider, WorkspaceRunnerProvider)
        assert provider.name == "runner"
        assert provider_mod.get_provider("runner").name == "runner"
    finally:
        get_settings.cache_clear()


async def test_resolve_provider_name_legacy_env_alias(monkeypatch, audit_db):
    """CODEGEN_PROVIDER=kiro (pre-rename deployments) resolves to `runner`
    both under the cloud pin and as the local bootstrap default."""
    from app.core.config import get_settings

    monkeypatch.setenv("CODEGEN_PROVIDER", "kiro")
    monkeypatch.setenv("ENVIRONMENT", "cloud")
    get_settings.cache_clear()
    try:
        assert await provider_mod.resolve_provider_name() == "runner"
        monkeypatch.setenv("ENVIRONMENT", "dev")
        get_settings.cache_clear()
        # audit_db points SessionLocal at a fresh test DB with no settings
        # row → the env bootstrap default applies, normalized.
        assert await provider_mod.resolve_provider_name() == "runner"
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------- pipeline


async def test_build_pipeline_ready(db_session, test_user, client_for, audit_db, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)
    project = await _project_with_docs(db_session, test_user)

    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 202, r.text
        build_id = uuid.UUID(r.json()["id"])
        # single active build per project
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 409

        build = await _wait_terminal(db_session, build_id)
        assert build.status == "ready", build.error
        assert build.content_hash and build.spec_hash
        assert build.manifest["files"] and build.manifest["validation"]["findings"] == []
        assert fake.calls == ["plan", "src/handler.py", "README.md", "template.json"]
        assert fake.planned_profiles == ["inline-cfn"]  # 13.5Z: profile reaches the planner

        # artifacts persisted + retrievable
        r = await client.get(f"/api/v1/builds/{build_id}/artifacts")
        paths = [a["path"] for a in r.json()["items"]]
        assert sorted(paths) == ["README.md", "src/handler.py", "template.json"]
        r = await client.get(f"/api/v1/builds/{build_id}/artifacts/src/handler.py")
        assert r.status_code == 200 and "handler" in r.json()["content"]

        # bundle zip layout (R6)
        r = await client.get(f"/api/v1/builds/{build_id}/bundle")
        assert r.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        names = archive.namelist()
        assert any(n.startswith(".kiro/specs/") and n.endswith("requirements.md") for n in names)
        assert "generated/template.json" in names
        assert "build-manifest.json" in names
        manifest = json.loads(archive.read("build-manifest.json"))
        assert manifest["build_id"] == str(build_id)

        # history list
        r = await client.get(f"/api/v1/projects/{project.id}/builds")
        assert [b["status"] for b in r.json()["items"]] == ["ready"]


async def test_build_validation_failure(db_session, test_user, client_for, audit_db, monkeypatch):
    bad_template = json.dumps(
        {"Resources": {"Bad": {"Type": "AWS::EC2::Instance", "Properties": {}}}, "Outputs": {}}
    )
    fake = FakeProvider(template=bad_template)
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "failed"
        assert build.error["code"] == "validation_failed"
        assert any(f["check"] == "service_allowlist" for f in build.error["findings"])
        # bundle refused for non-ready builds
        r = await client.get(f"/api/v1/builds/{build.id}/bundle")
        assert r.status_code == 409


async def test_build_requires_docs_and_editor_role(db_session, test_user, client_for):
    empty = Project(user_id=test_user.id, name="Empty", status="draft")
    viewer = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="bviewer@marshal.demo",
        role="power", persona="power", onboarding_completed=True,
    )
    db_session.add_all([empty, viewer])
    await db_session.commit()
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{empty.id}/builds")
        assert r.status_code == 409  # no docs
    from app.models import ProjectMember

    project = await _project_with_docs(db_session, test_user)
    db_session.add(ProjectMember(project_id=project.id, user_id=viewer.id, role="viewer"))
    await db_session.commit()
    async with client_for(viewer) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 403  # viewers read-only
        r = await client.get(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 200  # but they can see builds


async def test_rehydrate_fails_inflight(db_session, test_user, audit_db):
    project = await _project_with_docs(db_session, test_user)
    build = CodegenBuild(project_id=project.id, status="generating", created_by=test_user.id)
    db_session.add(build)
    await db_session.commit()
    await runner.rehydrate_inflight_builds()
    await db_session.refresh(build)
    assert build.status == "failed" and build.error["code"] == "restarted"


async def test_cap_breach_fails_build(db_session, test_user, client_for, audit_db, monkeypatch):
    """Governance mid-build: cap/limit errors become failed builds w/ code (R7.2)."""

    class CapBlowingProvider(FakeProvider):
        async def generate_file(self, ctx, plan, file, generated):
            from app.services.spend import CostCapExceeded

            raise CostCapExceeded(scope="user", cap=10, mtd=11)

    monkeypatch.setattr(runner, "get_provider", lambda name=None: CapBlowingProvider())
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "failed" and build.error["code"] == "cost_cap"


async def test_deploy_uses_build_template(db_session, test_user, client_for, audit_db, monkeypatch):
    """R5: build_id deploys the artifact payload; fallback stays the sample app."""
    fake = FakeProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
        assert build.status == "ready"

    from app.services import deployment as dep_svc

    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, build_id=build.id,
        status="pending", timeline=[], resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()
    payload = await dep_svc._resolve_payload(db_session, deployment)
    assert json.loads(payload.template)["Outputs"]["ApiUrl"]  # the build's template
    assert payload.assets == []  # inline-cfn has no staged assets

    # fallback: no build_id → sample app path
    plain = Deployment(
        project_id=project.id, user_id=test_user.id, status="pending", timeline=[], resources=[]
    )
    db_session.add(plain)
    await db_session.commit()
    sample = await dep_svc._resolve_payload(db_session, plain)
    assert "marshal" in sample.template or "Resources" in sample.template

    # API guard: build must be ready + on the project
    other = Project(user_id=test_user.id, name="Other", status="draft")
    db_session.add(other)
    await db_session.commit()
    async with client_for(test_user) as client:
        r = await client.post(
            f"/api/v1/projects/{other.id}/deploy", json={"build_id": str(build.id)}
        )
        assert r.status_code == 404


async def test_artifact_rows_have_hashes(db_session, test_user, client_for, audit_db, monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    rows = (
        (await db_session.execute(select(CodegenArtifact).where(CodegenArtifact.build_id == build.id)))
        .scalars()
        .all()
    )
    assert all(len(a.content_hash) == 64 and a.size_bytes > 0 for a in rows)


# ---------------------------------------------- C1 connector-lite (signal + gate)


def test_connector_signal_detection_is_narrow():
    from app.services.codegen.validate import declared_connectors

    text = (
        "The agent SHALL use connector support-api for ticket lookups.\n"
        "It also SHALL use connector Billing-DB.\n"
        "Duplicate: the agent SHALL use connector support-api again.\n"
    )
    assert declared_connectors(text) == ["billing-db", "support-api"]
    # Prose about connectors is NOT a declaration
    assert declared_connectors("The team uses connectors for everything") == []
    assert declared_connectors("shall use the connector registry") == []
    assert declared_connectors("") == []
    # Cap at 5, deterministic order
    many = "\n".join(f"SHALL use connector c-{i}" for i in range(9))
    assert len(declared_connectors(many)) == 5


def _connector_template(env: dict | None, iam_slugs: list[str]) -> dict:
    policies = [
        {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "secretsmanager:GetSecretValue",
                            "Resource": {
                                "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:"
                                f"${{AWS::AccountId}}:secret:marshal/agent-connectors/{slug}*"
                            },
                        }
                        for slug in iam_slugs
                    ]
                }
            },
        }
    ]
    resources = {
        "Fn": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Code": {"ZipFile": "def handler(e, c): pass"},
                "Environment": {"Variables": env or {}},
            },
        }
    }
    for i, policy in enumerate(policies):
        if iam_slugs:
            resources[f"Pol{i}"] = policy
    return {"Resources": resources}


def test_connector_gate_clean_pass_and_wiring():
    from app.services.codegen.validate import _connector_findings

    doc = _connector_template(
        {"CONNECTOR_SUPPORT_API_SECRET": "marshal/agent-connectors/support-api"},
        ["support-api"],
    )
    assert _connector_findings(doc, {}, ["support-api"]) == []

    # Declared but not wired: missing env var
    bare = _connector_template({}, ["support-api"])
    findings = _connector_findings(bare, {}, ["support-api"])
    assert any("not wired" in f.message for f in findings)

    # Wrong secret name value
    wrong = _connector_template(
        {"CONNECTOR_SUPPORT_API_SECRET": "marshal/connectors/support-api"},
        ["support-api"],
    )
    findings = _connector_findings(wrong, {}, ["support-api"])
    assert any("must be exactly" in f.message for f in findings)

    # Missing IAM read grant
    no_iam = _connector_template(
        {"CONNECTOR_SUPPORT_API_SECRET": "marshal/agent-connectors/support-api"}, []
    )
    findings = _connector_findings(no_iam, {}, ["support-api"])
    assert any("read" in f.message and "grant" in f.message for f in findings)


def test_connector_gate_refuses_undeclared_reach():
    from app.services.codegen.validate import _connector_findings

    # Undeclared env var on a zero-declaration build
    doc = _connector_template(
        {"CONNECTOR_ROGUE_SECRET": "marshal/agent-connectors/rogue"}, ["rogue"]
    )
    findings = _connector_findings(doc, {}, [])
    assert findings and all(f.check == "connector_contract" for f in findings)

    # Undeclared slug referenced from CODE while another is declared
    doc = _connector_template(
        {"CONNECTOR_SUPPORT_API_SECRET": "marshal/agent-connectors/support-api"},
        ["support-api"],
    )
    artifacts = {"src/app.py": 'sid = "marshal/agent-connectors/exfil-target"'}
    findings = _connector_findings(doc, artifacts, ["support-api"])
    assert any("exfil-target" in f.message for f in findings)


async def test_declared_connectors_flag_gated(monkeypatch):
    """connectors_enabled off ⇒ the phrase is inert prose (C0 honesty).
    The settings read is stubbed here; the real end-to-end flag path is
    proven by test_integrations.test_deploy_preflight_connector_refusals."""
    from types import SimpleNamespace

    from app.services import platform_settings as settings_svc
    from app.services.codegen.runner import _spec_declared_connectors

    build = CodegenBuild(
        project_id=uuid.uuid4(),
        status="queued",
        spec_snapshot={
            "requirements": {"content": "The agent SHALL use connector support-api."}
        },
    )
    flags = {"connectors_enabled": False}

    async def fake_controls():
        return SimpleNamespace(feature_flags=dict(flags))

    monkeypatch.setattr(settings_svc, "get_controls", fake_controls)
    assert await _spec_declared_connectors(build) == []

    flags["connectors_enabled"] = True
    assert await _spec_declared_connectors(build) == ["support-api"]


# --------------------------------------------- C3 v1: MCP tool-loop agents


def test_mcp_signal_and_union():
    from app.services.codegen.validate import declared_connectors, mcp_tool_connectors

    text = (
        "The agent SHALL use MCP tools from connector Docs-MCP.\n"
        "It also SHALL use connector billing-db for lookups.\n"
    )
    assert mcp_tool_connectors(text) == ["docs-mcp"]
    # Prose never declares
    assert mcp_tool_connectors("uses MCP tools sometimes") == []
    # C1 phrase does not trip the MCP signal and vice versa
    assert mcp_tool_connectors("SHALL use connector docs-mcp") == []
    assert declared_connectors(text) == ["billing-db"]


def test_mcp_scaffold_fits_inline_budget_and_compiles():
    from app.services.codegen.scaffolds import MCP_AGENT_SCAFFOLD
    from app.services.codegen.validate import MAX_INLINE_CHARS

    assert len(MCP_AGENT_SCAFFOLD) < MAX_INLINE_CHARS - 100  # real headroom
    compile(MCP_AGENT_SCAFFOLD, "mcp_agent.py", "exec")
    # The gate's own anchors stay present
    assert "MAX_TOOL_ITERATIONS = 5" in MCP_AGENT_SCAFFOLD
    assert "MCP_CONNECTOR_SLUG" in MCP_AGENT_SCAFFOLD


def _mcp_template(slug="docs-mcp", *, env_overrides=None, code=None):
    from app.services.codegen.scaffolds import MCP_AGENT_SCAFFOLD

    env = {
        "MCP_CONNECTOR_SLUG": slug,
        f"CONNECTOR_{slug.upper().replace('-', '_')}_SECRET": f"marshal/agent-connectors/{slug}",
        "AGENT_SYSTEM_PROMPT": "You answer questions about internal docs.",
    }
    env.update(env_overrides or {})
    return {
        "Resources": {
            "AgentFn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {"ZipFile": code if code is not None else MCP_AGENT_SCAFFOLD},
                    "Environment": {"Variables": env},
                },
            },
            "Pol": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "secretsmanager:GetSecretValue",
                                "Resource": {
                                    "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:"
                                    "${AWS::Region}:${AWS::AccountId}:secret:"
                                    f"marshal/agent-connectors/{slug}*"
                                },
                            }
                        ]
                    }
                },
            },
        }
    }


def test_mcp_tool_loop_gate_matrix():
    from app.services.codegen.scaffolds import MCP_AGENT_FILENAME, MCP_AGENT_SCAFFOLD
    from app.services.codegen.validate import _mcp_tool_loop_findings

    artifacts = {MCP_AGENT_FILENAME: MCP_AGENT_SCAFFOLD}

    # Clean pass
    assert _mcp_tool_loop_findings(_mcp_template(), artifacts, ["docs-mcp"]) == []

    # Tampered scaffold (one byte) — refused on byte-equality
    tampered = {MCP_AGENT_FILENAME: MCP_AGENT_SCAFFOLD.replace(
        "MAX_TOOL_ITERATIONS = 5", "MAX_TOOL_ITERATIONS = 50"
    )}
    doc = _mcp_template(code=tampered[MCP_AGENT_FILENAME])
    findings = _mcp_tool_loop_findings(doc, tampered, ["docs-mcp"])
    assert any("BYTE-EQUAL" in f.message for f in findings)

    # Wrong slug env
    doc = _mcp_template(env_overrides={"MCP_CONNECTOR_SLUG": "other"})
    findings = _mcp_tool_loop_findings(doc, artifacts, ["docs-mcp"])
    assert any("MCP_CONNECTOR_SLUG must be exactly" in f.message for f in findings)

    # Missing behavior prompt
    doc = _mcp_template(env_overrides={"AGENT_SYSTEM_PROMPT": " "})
    findings = _mcp_tool_loop_findings(doc, artifacts, ["docs-mcp"])
    assert any("AGENT_SYSTEM_PROMPT" in f.message for f in findings)

    # Two declared — v1 refuses
    findings = _mcp_tool_loop_findings(
        _mcp_template(), artifacts, ["a-mcp", "b-mcp"]
    )
    assert any("exactly ONE" in f.message for f in findings)

    # Scaffold present without declaration — refused
    findings = _mcp_tool_loop_findings(_mcp_template(), artifacts, [])
    assert any("declares no MCP connector" in f.message for f in findings)


async def test_spec_connector_sets_union_and_flag(monkeypatch):
    from types import SimpleNamespace

    from app.services import platform_settings as settings_svc
    from app.services.codegen.runner import _spec_connector_sets

    build = CodegenBuild(
        project_id=uuid.uuid4(),
        status="queued",
        spec_snapshot={
            "requirements": {
                "content": (
                    "SHALL use connector billing-db.\n"
                    "SHALL use MCP tools from connector docs-mcp."
                )
            }
        },
    )
    flags = {"connectors_enabled": True}

    async def fake_controls():
        return SimpleNamespace(feature_flags=dict(flags))

    monkeypatch.setattr(settings_svc, "get_controls", fake_controls)
    union, mcp = await _spec_connector_sets(build)
    assert union == ["billing-db", "docs-mcp"]  # custody covers both
    assert mcp == ["docs-mcp"]

    flags["connectors_enabled"] = False
    assert await _spec_connector_sets(build) == ([], [])


# ------------------------------------------- composable agents gate (13.5R)


def _dep_template(slug, *, env=None, iam=True, agent_id=True):
    variables = {
        f"AGENT_DEP_{slug.upper().replace('-', '_')}_SECRET": f"marshal/agent-dependencies/{slug}",
    }
    if agent_id:
        variables["MARSHAL_AGENT_ID"] = str(uuid.uuid4())
    variables.update(env or {})
    resources = {
        "Fn": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Code": {"ZipFile": f'call_agent("{slug}", "run", x)'},
                "Environment": {"Variables": variables},
            },
        }
    }
    if iam:
        resources["Pol"] = {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "secretsmanager:GetSecretValue",
                            "Resource": {
                                "Fn::Sub": "arn:${AWS::Partition}:secretsmanager:${AWS::Region}:"
                                f"${{AWS::AccountId}}:secret:marshal/agent-dependencies/{slug}*"
                            },
                        }
                    ]
                }
            },
        }
    return {"Resources": resources}


def test_agent_dependency_gate_matrix():
    from app.services.codegen.validate import _agent_dependency_findings

    slug = "support-bot-1a2b3c4d"
    code = {"src/app.py": f'call_agent("{slug}", "run", x)'}

    # Clean pass
    assert _agent_dependency_findings(_dep_template(slug), code, [slug], False) == []

    # Undeclared reach (env var present, nothing declared)
    findings = _agent_dependency_findings(_dep_template(slug), code, [], False)
    assert findings and all(f.check == "agent_dependency" for f in findings)

    # Declared but unwired (no env)
    bare = {"Resources": {"Fn": {"Type": "AWS::Lambda::Function", "Properties": {
        "Code": {"ZipFile": "x"}, "Environment": {"Variables": {}}}}}}
    findings = _agent_dependency_findings(bare, {}, [slug], False)
    assert any("not wired" in f.message for f in findings)

    # Missing IAM grant
    findings = _agent_dependency_findings(
        _dep_template(slug, iam=False), code, [slug], False
    )
    assert any("read grant" in f.message for f in findings)

    # Orchestrated pipeline must CALL every declared agent
    other = "intake-9f8e7d6c"
    doc = _dep_template(slug)
    doc["Resources"]["Fn2"] = {
        "Type": "AWS::Lambda::Function",
        "Properties": {"Code": {"ZipFile": "y"}, "Environment": {"Variables": {
            f"AGENT_DEP_{other.upper().replace('-', '_')}_SECRET": f"marshal/agent-dependencies/{other}",
        }}},
    }
    doc["Resources"]["Pol2"] = {
        "Type": "AWS::IAM::Policy",
        "Properties": {"PolicyDocument": {"Statement": [{
            "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
            "Resource": {"Fn::Sub": f"arn:x:secret:marshal/agent-dependencies/{other}*"}}]}},
    }
    findings = _agent_dependency_findings(doc, code, [slug, other], True)
    assert any("never calls declared agent" in f.message for f in findings)


# ---------------------------------------- agent-substance memory rung (13.5T)


def _memory_template(*, ttl=True, keys=("session_id", "sk"), env=True):
    resources = {
        "AgentMemoryTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": [
                    {"AttributeName": keys[0], "KeyType": "HASH"},
                    {"AttributeName": keys[1], "KeyType": "RANGE"},
                ],
                "TimeToLiveSpecification": {
                    "AttributeName": "expires_at" if ttl else "other",
                    "Enabled": bool(ttl),
                },
            },
        },
        "Fn": {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Code": {"ZipFile": "x"},
                "Environment": {
                    "Variables": {"MEMORY_TABLE": {"Ref": "AgentMemoryTable"}} if env else {}
                },
            },
        },
    }
    return {"Resources": resources}


MEMORY_CODE = {"src/app.py": "def remember(a, b, c): ...\ndef recall(a): ..."}


def test_memory_signal_is_narrow():
    from app.services.codegen.validate import requires_memory

    assert requires_memory("The agent SHALL keep conversation memory.")
    assert requires_memory("it shall KEEP Conversation Memory across turns")
    assert not requires_memory("the agent remembers things sometimes")
    assert not requires_memory("keep conversation logs")
    assert not requires_memory("")


def test_memory_gate_matrix():
    from app.services.codegen.validate import _memory_findings

    # Clean pass
    assert _memory_findings(_memory_template(), MEMORY_CODE, True) == []

    # Declared, missing table
    findings = _memory_findings({"Resources": {}}, MEMORY_CODE, True)
    assert any("AgentMemoryTable" in f.message for f in findings)

    # TTL disabled / wrong attribute
    findings = _memory_findings(_memory_template(ttl=False), MEMORY_CODE, True)
    assert any("TimeToLiveSpecification" in f.message for f in findings)

    # Wrong key schema
    findings = _memory_findings(
        _memory_template(keys=("user_id", "ts")), MEMORY_CODE, True
    )
    assert any("key schema" in f.message for f in findings)

    # Missing env wiring
    findings = _memory_findings(_memory_template(env=False), MEMORY_CODE, True)
    assert any("MEMORY_TABLE" in f.message for f in findings)

    # Helpers absent from code
    findings = _memory_findings(_memory_template(), {"src/app.py": "pass"}, True)
    assert any("remember/recall" in f.message for f in findings)

    # Symmetry: memory artifacts WITHOUT declaration are refused
    findings = _memory_findings(_memory_template(), MEMORY_CODE, False)
    assert any("does not declare" in f.message for f in findings)
    # And a clean undeclared build passes
    assert _memory_findings({"Resources": {}}, {}, False) == []


# ---------------------- agent substance R1: packaged profile (task 2)


def test_packaged_signal_is_narrow():
    from app.services.codegen.validate import requires_packaged

    assert requires_packaged("The agent SHALL use packaged dependencies.")
    assert requires_packaged("it shall use packaged dependencies")
    assert not requires_packaged("We may use packaged dependencies")
    assert not requires_packaged("SHALL use packaged dependency")
    assert not requires_packaged("")


async def test_packaged_profile_resolution(db_session, test_user):
    """The signal is the ONLY door in; conflicts refuse loudly (R1.2)."""
    project = await _project_with_docs(db_session, test_user)
    declared = "# Req\nThe agent SHALL use packaged dependencies.\n"

    assert (
        await runner._resolve_profile(db_session, project, None, declared)
        == "packaged-cfn"
    )
    assert (
        await runner._resolve_profile(db_session, project, "packaged-cfn", declared)
        == "packaged-cfn"
    )
    with pytest.raises(runner.ProfileUnavailable, match="cannot override"):
        await runner._resolve_profile(db_session, project, "inline-cfn", declared)
    with pytest.raises(runner.ProfileUnavailable, match="spec-declared"):
        await runner._resolve_profile(db_session, project, "packaged-cfn", "# Req\n")
    assert (
        await runner._resolve_profile(db_session, project, None, "# Req\n")
        == "inline-cfn"
    )


async def test_packaged_profile_reaches_the_planner(
    db_session, test_user, client_for, audit_db, monkeypatch
):
    """13.5Z live defect: the in-process build resolved packaged-cfn correctly
    but called `provider.plan(ctx)` WITHOUT the profile, so the planner ran
    the inline prompt and never force-inserted requirements.txt — every real
    packaged build died at the gate. The external runner always passed it;
    this pins the in-process call site to the resolved profile."""
    fake = FakeProvider()
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)
    monkeypatch.setattr(
        runner.get_settings(), "codegen_workspace_bucket", "probe-bucket", raising=False
    )
    project = await _project_with_docs(db_session, test_user)
    req = (await db_session.execute(
        select(Spec).where(Spec.project_id == project.id, Spec.type == "requirements")
    )).scalar_one()
    req.content += "\nThe agent SHALL use packaged dependencies.\n"
    await db_session.commit()

    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 202, r.text
        assert r.json()["artifact_profile"] == "packaged-cfn"
        build_id = uuid.UUID(r.json()["id"])
        build = await _wait_terminal(db_session, build_id)

    # The stub plans an inline-shaped file set, so the packaged gate refuses
    # (no requirements.txt) — fine: the assertion is what the planner was TOLD.
    assert build.status in ("failed", "ready")
    assert fake.planned_profiles == ["packaged-cfn"]


def test_requirements_pin_matrix():
    from app.services.codegen import contract

    assert contract.requirements_pin_errors("requests==2.32.5\n# comment\n") == []
    assert contract.requirements_pin_errors("pydantic[email]==2.11.7\n") == []
    errors = contract.requirements_pin_errors("requests>=2.0\n")
    assert errors and "exact pin" in errors[0]
    # 13.5Z: an EMPTY manifest is valid — packaged-for-size apps (handlers over
    # the 4 KB inline ceiling, stdlib + boto3 only) declare nothing.
    assert contract.requirements_pin_errors("") == []
    assert contract.requirements_pin_errors("# nothing beyond the runtime\n") == []
    too_many = "\n".join(f"pkg{i}==1.0.0" for i in range(9))
    assert any("max 8" in e for e in contract.requirements_pin_errors(too_many))
    assert contract.requirements_pin_errors("-e git+https://evil.example/x.git\n")


def test_third_party_imports_derivation():
    """13.5Z: requirements.txt content is DERIVED from handler imports."""
    from app.services.codegen.internal import pin_lines_only, third_party_imports

    stdlib_only = {
        "src/a.py": "import json\nimport os\nfrom datetime import datetime\nimport boto3\n",
        "src/b.py": "from botocore.exceptions import ClientError\nimport urllib.request\n",
        "README.md": "import requests  # prose in docs must not count\n",
    }
    assert third_party_imports(stdlib_only) == []
    mixed = {
        "src/a.py": "import json\nimport requests\nfrom pydantic import BaseModel\n",
        "src/b.py": "import yaml\nfrom . import helpers\nimport requests\n",
    }
    assert third_party_imports(mixed) == ["pydantic", "requests", "yaml"]

    # The model's manifest answer is filtered to exact pins — prose can never
    # reach the deliverable (live: "Looking at the handlers described…").
    narrated = (
        "Looking at the handlers described, they use:\n"
        "- boto3 (DynamoDB + S3)\n"
        "```\nrequests==2.32.5\nPyYAML==6.0.2\n```\n"
        "That's all you need.\n"
    )
    assert pin_lines_only(narrated) == "requests==2.32.5\nPyYAML==6.0.2\n"
    assert pin_lines_only("no pins here at all") == ""


async def test_packaged_requirements_empty_without_model_call(monkeypatch):
    """Stdlib + boto3 handlers → empty manifest, ZERO model calls (13.5Z)."""
    from app.services.codegen import internal

    async def must_not_call(**kwargs):
        raise AssertionError("no model call expected for a runtime-only manifest")

    monkeypatch.setattr(internal, "converse", must_not_call)
    ctx = BuildCtx(
        build_id=uuid.uuid4(), project_id=uuid.uuid4(), project_name="P",
        user_id=None, spec_docs={}, model_id="m", max_tokens=1000,
        require_packaged=True,
    )
    provider = internal.InternalProvider()
    plan = BuildPlan(app_name="a", architecture_notes="n", files=[
        PlannedFile(path="src/h.py", brief="h"),
        PlannedFile(path="requirements.txt", brief="deps", language="text"),
    ])
    generated = {"src/h.py": "import json\nimport boto3\ndef handler(e, c):\n    return {}\n"}
    out = await provider.generate_file(
        ctx, plan, PlannedFile(path="requirements.txt", brief="deps", language="text"),
        generated,
    )
    assert out == ""


async def test_packaged_requirements_pins_third_party(monkeypatch):
    """Third-party imports → one small pin-only model call, filtered (13.5Z)."""
    from app.services.codegen import internal

    seen: dict = {}

    async def fake_converse(**kwargs):
        seen["prompt"] = kwargs["messages"][0]["content"][0]["text"]
        seen["max_tokens"] = kwargs["max_tokens"]
        return "Sure! Here you go:\nrequests==2.32.5\n", {}, "end_turn"

    monkeypatch.setattr(internal, "converse", fake_converse)
    ctx = BuildCtx(
        build_id=uuid.uuid4(), project_id=uuid.uuid4(), project_name="P",
        user_id=None, spec_docs={}, model_id="m", max_tokens=1000,
        require_packaged=True,
    )
    provider = internal.InternalProvider()
    plan = BuildPlan(app_name="a", architecture_notes="n", files=[])
    out = await provider.generate_file(
        ctx, plan, PlannedFile(path="requirements.txt", brief="deps", language="text"),
        {"src/h.py": "import requests\nimport json\n"},
    )
    assert out == "requests==2.32.5\n"
    assert "requests" in seen["prompt"] and seen["max_tokens"] == 300


def test_package_results_contract():
    from app.services.codegen import contract

    good = {
        "contract_version": 1,
        "status": "succeeded",
        "zip_sha256": "a" * 64,
        "zip_bytes": 1024,
        "packages": [{"name": "requests", "version": "2.32.5", "license": "Apache-2.0"}],
    }
    parsed = contract.parse_package_results(good)
    assert parsed.status == "succeeded" and parsed.packages[0]["name"] == "requests"

    failed = contract.parse_package_results(
        {"contract_version": 1, "status": "failed", "error": {"code": "pip_failed", "message": "x"}}
    )
    assert failed.status == "failed" and failed.error["code"] == "pip_failed"

    with pytest.raises(contract.ContractViolation, match="zip_sha256"):
        contract.parse_package_results(dict(good, zip_sha256="short"))
    with pytest.raises(contract.ContractViolation, match="over the"):
        contract.parse_package_results(dict(good, zip_bytes=contract.MAX_PACKAGE_ZIP_BYTES + 1))
    with pytest.raises(contract.ContractViolation):
        contract.parse_package_results({"contract_version": 2, "status": "succeeded"})


def _packaged_template(*, params=True, handler="handler.handler"):
    doc = {
        "Parameters": {
            "PackageBucket": {"Type": "String"},
            "PackageKey": {"Type": "String"},
        },
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {
                        "S3Bucket": {"Ref": "PackageBucket"},
                        "S3Key": {"Ref": "PackageKey"},
                    },
                    "Handler": handler,
                    "Runtime": "python3.12",
                    "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                },
            },
            "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
        },
        "Outputs": {"ApiUrl": {"Value": "https://example"}},
    }
    if not params:
        doc.pop("Parameters")
    return doc


def _packaged_artifacts(doc, requirements="requests==2.32.5\n"):
    return {
        "template.json": json.dumps(doc),
        "src/handler.py": "import requests\ndef handler(event, context):\n    return {}\n",
        "requirements.txt": requirements,
        "README.md": "# Demo\n",
    }


def test_packaged_gate_matrix():
    # Green: params + package refs + module handler + pinned manifest
    findings, _ = validate_artifacts(
        _packaged_artifacts(_packaged_template()),
        [],
        profile="packaged-cfn",
        require_packaged=True,
    )
    assert findings == []

    # Missing parameters
    findings, _ = validate_artifacts(
        _packaged_artifacts(_packaged_template(params=False)),
        [], profile="packaged-cfn", require_packaged=True,
    )
    assert any(f.check == "packaged_dependencies" and "Parameters" in f.message for f in findings)

    # Handler must be <module>.handler
    findings, _ = validate_artifacts(
        _packaged_artifacts(_packaged_template(handler="index.lambda_entry")),
        [], profile="packaged-cfn", require_packaged=True,
    )
    assert any("Handler" in f.message for f in findings)

    # requirements.txt pin discipline rides the gate
    findings, _ = validate_artifacts(
        _packaged_artifacts(_packaged_template(), requirements="requests>=2\n"),
        [], profile="packaged-cfn", require_packaged=True,
    )
    assert any(f.check == "packaged_dependencies" and "exact pin" in f.message for f in findings)

    # Wrong code shape (plain strings instead of the parameter refs)
    doc = _packaged_template()
    doc["Resources"]["Fn"]["Properties"]["Code"] = {"S3Bucket": "b", "S3Key": "k"}
    findings, _ = validate_artifacts(
        _packaged_artifacts(doc), [], profile="packaged-cfn", require_packaged=True,
    )
    assert any("staged" in f.message for f in findings)

    # Symmetry: declared but built inline
    findings, _ = validate_artifacts(
        dict(GOOD_ARTIFACTS), [], profile="inline-cfn", require_packaged=True,
    )
    assert any(f.check == "packaged_profile" for f in findings)
    # Symmetry: packaged build without the declaration
    findings, _ = validate_artifacts(
        _packaged_artifacts(_packaged_template()),
        [], profile="packaged-cfn", require_packaged=False,
    )
    assert any(f.check == "packaged_profile" for f in findings)


async def test_payload_for_packaged_build(db_session, test_user):
    from app.services import deployment as deploy_svc

    project = await _project_with_docs(db_session, test_user)
    build = CodegenBuild(
        project_id=project.id, status="ready", provider="internal",
        artifact_profile="packaged-cfn", spec_snapshot={}, spec_hash="x",
        created_by=test_user.id,
    )
    db_session.add(build)
    await db_session.flush()
    db_session.add(
        CodegenArtifact(
            build_id=build.id, path="template.json",
            content=json.dumps(_packaged_template()),
            content_hash="t" * 64, size_bytes=10, language="json",
        )
    )
    db_session.add(
        CodegenArtifact(
            build_id=build.id, path="package.zip", content=None,
            s3_key=f"artifacts/{build.id}/package.zip",
            content_hash="c" * 64, size_bytes=2048, language="binary",
        )
    )
    await db_session.commit()

    payload = await deploy_svc._payload_for_build(db_session, build.id)
    assert len(payload.assets) == 1
    asset = payload.assets[0]
    assert asset["bucket_parameter"] == "PackageBucket"
    assert asset["plain_key"] is True
    assert asset["staged_key"] == f"marshal-assets/{'c' * 64}.zip"

    # Parameter binding: plain key for packaged, '||' suffix for cdk assets
    params = deploy_svc._asset_parameters(asset, "marshal-assets-123")
    values = {p["ParameterKey"]: p["ParameterValue"] for p in params}
    assert values["PackageKey"] == asset["staged_key"]  # no suffix
    assert "PackageHash" not in values
    cdk_asset = {
        "staged_key": "marshal-assets/h.zip", "source_hash": "h",
        "bucket_parameter": "B", "key_parameter": "K", "hash_parameter": "H",
    }
    cdk_values = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in deploy_svc._asset_parameters(cdk_asset, "b")
    }
    assert cdk_values["K"].endswith("||") and cdk_values["H"] == "h"


# ------------------- agent substance R2.2/R2.3: tools + planning (tasks 3+4)


def test_tools_and_planning_signal_detection():
    from app.services.codegen.validate import declared_tools, requires_planning

    assert declared_tools("SHALL use tools: convert_units, lookup-rate") == [
        "convert_units", "lookup_rate",
    ]
    # dedupe + sort + cap + invalid names dropped
    many = "SHALL use tools: " + ", ".join(f"t{i}" for i in range(9))
    assert len(declared_tools(many)) == 6
    assert declared_tools("SHALL use tools: 9bad, x") == ["x"] or declared_tools(
        "SHALL use tools: 9bad, x"
    ) == []  # leading digit invalid; 'x' is too short (min 2 chars)
    assert declared_tools("uses some tools sometimes") == []

    assert requires_planning("The agent SHALL plan multi-step responses.")
    assert not requires_planning("planning is nice")


def test_loop_cap_rail():
    from types import SimpleNamespace

    assert runner._loop_cap(None) == 5
    tpl = SimpleNamespace(guardrails={"capabilities": {"max_loop_iterations": 3}})
    assert runner._loop_cap(tpl) == 3
    assert runner._loop_cap(SimpleNamespace(guardrails={"capabilities": {"max_loop_iterations": 99}})) == 8
    assert runner._loop_cap(SimpleNamespace(guardrails={"capabilities": {"max_loop_iterations": "x"}})) == 5


def _tools_agent_file(declared):
    from app.services.codegen.scaffolds import assemble_tools_agent

    bodies = "\n".join(
        f'def tool_{name}(args):\n    return {{"tool": "{name}", "input": args.get("input")}}'
        for name in declared
    )
    return assemble_tools_agent(declared, bodies)


def _loop_template(code, env, *, iam_bedrock=True):
    return {
        "Resources": {
            "Agent": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {"ZipFile": code},
                    "Handler": "index.handler",
                    "Runtime": "python3.12",
                    "Environment": {"Variables": env},
                    "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                },
            },
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "p",
                            "PolicyDocument": {
                                "Statement": [
                                    {"Action": ["bedrock:InvokeModel"] if iam_bedrock else ["s3:GetObject"], "Effect": "Allow", "Resource": "*"}
                                ]
                            },
                        }
                    ]
                },
            },
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
        },
        "Outputs": {"ApiUrl": {"Value": "https://example"}},
    }


def test_tools_gate_matrix():
    declared = ["convert_units", "lookup_rate"]
    agent = _tools_agent_file(declared)
    assert len(agent) < 4000  # inline-compatible with room for bodies
    env = {"TOOLS_MAX_ITERATIONS": "5", "AGENT_SYSTEM_PROMPT": "Convert things."}
    artifacts = {
        "template.json": json.dumps(_loop_template(agent, env)),
        "src/tools_agent.py": agent,
        "README.md": "# Demo\n",
    }
    findings, _ = validate_artifacts(artifacts, [], declared_tools=declared)
    assert findings == []

    # Prelude drift (TOOLS_JSON for a different tool list) is refused
    wrong = dict(artifacts, **{"src/tools_agent.py": _tools_agent_file(["other_tool"])})
    findings, _ = validate_artifacts(wrong, [], declared_tools=declared)
    assert any(f.check == "tools_contract" and "BYTE-EQUAL" in f.message for f in findings)

    # A declared tool without an implementation is refused
    partial = _tools_agent_file(declared).replace("def tool_lookup_rate(", "def lookup_rate(")
    findings, _ = validate_artifacts(
        dict(artifacts, **{"src/tools_agent.py": partial}), [], declared_tools=declared
    )
    assert any("tool_lookup_rate" in f.message for f in findings)

    # Loop cap outside the template rail is refused
    bad_env = dict(env, TOOLS_MAX_ITERATIONS="12")
    bad = dict(
        artifacts, **{"template.json": json.dumps(_loop_template(agent, bad_env))}
    )
    findings, _ = validate_artifacts(bad, [], declared_tools=declared, loop_iteration_cap=5)
    assert any(f.check == "loop_caps" for f in findings)

    # Undeclared tool artifacts are refused symmetrically
    findings, _ = validate_artifacts(artifacts, [], declared_tools=[])
    assert any(f.check == "tools_contract" for f in findings)

    # Missing bedrock IAM is refused
    no_iam = dict(
        artifacts,
        **{"template.json": json.dumps(_loop_template(agent, env, iam_bedrock=False))},
    )
    findings, _ = validate_artifacts(no_iam, [], declared_tools=declared)
    assert any("bedrock:InvokeModel" in f.message for f in findings)


def test_planning_gate_matrix():
    from app.services.codegen.scaffolds import (
        PLANNING_AGENT_SCAFFOLD,
    )

    assert len(PLANNING_AGENT_SCAFFOLD) < 4000
    compile(PLANNING_AGENT_SCAFFOLD, "planning_agent.py", "exec")

    env = {"PLANNING_MAX_ITERATIONS": "4", "AGENT_SYSTEM_PROMPT": "Plan well."}
    artifacts = {
        "template.json": json.dumps(_loop_template(PLANNING_AGENT_SCAFFOLD, env)),
        "src/planning_agent.py": PLANNING_AGENT_SCAFFOLD,
        "README.md": "# Demo\n",
    }
    findings, _ = validate_artifacts(artifacts, [], require_planning=True, loop_iteration_cap=5)
    assert findings == []

    # Content drift is refused (full byte equality)
    drift = dict(artifacts, **{"src/planning_agent.py": PLANNING_AGENT_SCAFFOLD + "#\n"})
    findings, _ = validate_artifacts(drift, [], require_planning=True)
    assert any(f.check == "planning_contract" and "BYTE-EQUAL" in f.message for f in findings)

    # Undeclared planning artifacts are refused
    findings, _ = validate_artifacts(artifacts, [], require_planning=False)
    assert any(f.check == "planning_contract" for f in findings)


def test_rung_combination_refusals():
    from app.services.codegen.scaffolds import PLANNING_AGENT_SCAFFOLD

    env = {"PLANNING_MAX_ITERATIONS": "4", "AGENT_SYSTEM_PROMPT": "x"}
    artifacts = {
        "template.json": json.dumps(_loop_template(PLANNING_AGENT_SCAFFOLD, env)),
        "src/planning_agent.py": PLANNING_AGENT_SCAFFOLD,
        "README.md": "# Demo\n",
    }
    findings, _ = validate_artifacts(
        artifacts, [], declared_tools=["a_tool"], require_planning=True
    )
    assert any(f.check == "rung_combination" for f in findings)


def test_tools_scaffold_compiles():
    agent = _tools_agent_file(["convert_units"])
    compile(agent, "tools_agent.py", "exec")
    # The prelude's TOOLS_JSON is valid JSON with the declared names
    from app.services.codegen.scaffolds import tools_json_for

    spec = json.loads(tools_json_for(["convert_units"]))
    assert spec[0]["name"] == "convert_units"
    assert spec[0]["inputSchema"]["required"] == ["input"]
