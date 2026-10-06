"""CDK artifact profile tests (cdk-artifacts spec): profile plumbing, gate
extension, synth packaging, S3 offload, asset-based deploy payloads, bundle v2."""

import hashlib
import importlib.util
import io
import json
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import CodegenArtifact, CodegenBuild, Deployment, Project, Spec, Template, User
from app.services.codegen import contract, runner, storage
from app.services.codegen.validate import validate_artifacts
from app.services.codegen.workspace_runner import ExternalStatus
from tests.test_external_codegen import FakeWorkspaceProvider, _wait_terminal, _wire


@pytest.fixture()
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner, "POLL_INTERVAL_S", 0.02)

pytestmark = pytest.mark.asyncio

CDK_TEMPLATE = json.dumps(
    {
        "Parameters": {
            "AssetBucketParam": {"Type": "String"},
            "AssetKeyParam": {"Type": "String"},
            "AssetHashParam": {"Type": "String"},
        },
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {"S3Bucket": {"Ref": "AssetBucketParam"}, "S3Key": "resolved"},
                    "Handler": "index.handler",
                    "Runtime": "python3.12",
                    "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                },
            },
            "Role": {"Type": "AWS::IAM::Role", "Properties": {}},
            "Api": {"Type": "AWS::ApiGateway::RestApi", "Properties": {"Name": "x"}},
            "GetMethod": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"HttpMethod": "GET", "ApiKeyRequired": True},
            },
            "Key": {"Type": "AWS::ApiGateway::ApiKey", "Properties": {"Enabled": True}},
            "Plan": {
                "Type": "AWS::ApiGateway::UsagePlan",
                "Properties": {
                    "ApiStages": [{"ApiId": {"Ref": "Api"}, "Stage": "prod"}]
                },
            },
            "PlanKey": {
                "Type": "AWS::ApiGateway::UsagePlanKey",
                "Properties": {"KeyId": {"Ref": "Key"}, "KeyType": "API_KEY"},
            },
        },
        "Outputs": {
            "ApiUrl": {"Value": "https://example"},
            "ApiKeyId": {"Value": {"Ref": "Key"}},
        },
    }
)
PKG_JSON = json.dumps({"dependencies": {"aws-cdk-lib": "2.170.0", "constructs": "10.4.2"},
                       "devDependencies": {"typescript": "5.6.3"}})
CDK_TEXT_ARTIFACTS = {
    "lib/app-stack.ts": "import * as cdk from 'aws-cdk-lib';\nexport class AppStack extends cdk.Stack {}\n",
    "src/api/index.py": "def handler(event, context):\n    return {'statusCode': 200}\n",
    "README.md": "# App\nnpm ci && npx cdk deploy\n",
    "package.json": PKG_JSON,
    "cdk.json": "{}",
    "tsconfig.json": "{}",
    "bin/app.ts": "// templated\n",
    "synth/template.json": CDK_TEMPLATE,
}
ASSET_ZIP = b"PK\x05\x06" + b"\x00" * 18  # minimal empty zip


def _cdk_results():
    files = [
        contract.ResultFile(p, hashlib.sha256(c.encode()).hexdigest(), len(c.encode()))
        for p, c in sorted(CDK_TEXT_ARTIFACTS.items())
    ]
    files.append(
        contract.ResultFile(
            "synth/assets/abc123.zip", hashlib.sha256(ASSET_ZIP).hexdigest(),
            len(ASSET_ZIP), binary=True,
        )
    )
    return contract.Results(
        status="succeeded",
        files=files,
        usage=[contract.ResultUsage("us.anthropic.claude-sonnet-5", 3000, 1500)],
        engine={"name": "fake-engine", "version": "t"},
        app_name="cdk-probe",
        architecture_notes="cdk demo",
        synth_assets=[
            contract.SynthAsset(
                id="asset1", path="synth/assets/abc123.zip", source_hash="abc123",
                bucket_parameter="AssetBucketParam", key_parameter="AssetKeyParam",
                hash_parameter="AssetHashParam",
            )
        ],
    )


async def _project_with_docs(db, owner: User, template: Template | None = None) -> Project:
    project = Project(
        user_id=owner.id, name="CDK Buildable", status="spec_complete",
        template_id=template.id if template else None,
    )
    db.add(project)
    await db.flush()
    for doc_type in ("requirements", "design"):
        db.add(Spec(project_id=project.id, version=1, type=doc_type,
                    content=f"# {doc_type}\ncontent", created_by=owner.id))
    await db.commit()
    await db.refresh(project)
    return project


@pytest.fixture()
def fake_artifact_store(monkeypatch):
    """In-memory stand-in for the S3 artifacts prefix."""
    store: dict[str, bytes] = {}

    async def copy(*, build_id, attempt_root, paths):
        keys = {}
        for path in paths:
            key = storage.artifact_key(build_id, path)
            if path in CDK_TEXT_ARTIFACTS:
                store[key] = CDK_TEXT_ARTIFACTS[path].encode()
            else:
                store[key] = ASSET_ZIP
            keys[path] = key
        return keys

    async def by_key(s3_key: str) -> bytes:
        return store[s3_key]

    async def art_bytes(artifact) -> bytes:
        if artifact.content is not None:
            return artifact.content.encode()
        return store[artifact.s3_key]

    monkeypatch.setattr(storage, "copy_response_to_artifacts", copy)
    monkeypatch.setattr(storage, "artifact_bytes_by_key", by_key)
    monkeypatch.setattr(storage, "artifact_bytes", art_bytes)
    return store


# ------------------------------------------------------------------ gate


def test_gate_cdk_profile_matrix():
    keyed = {"mode": "key_required", "source": "default"}
    findings, _ = validate_artifacts(
        dict(CDK_TEXT_ARTIFACTS), [], profile="cdk-app", endpoint_auth=keyed
    )
    assert findings == []

    # B13 parity: synthesized CDK output is held to the same auth contract.
    doc = json.loads(CDK_TEMPLATE)
    del doc["Resources"]["Key"]
    del doc["Outputs"]["ApiKeyId"]
    missing = dict(CDK_TEXT_ARTIFACTS, **{"synth/template.json": json.dumps(doc)})
    findings, _ = validate_artifacts(
        missing, [], profile="cdk-app", endpoint_auth=keyed
    )
    assert any(f.check == "auth_contract" and "ApiKey resource" in f.message for f in findings)

    doc = json.loads(CDK_TEMPLATE)
    doc["Resources"]["GetMethod"]["Properties"]["ApiKeyRequired"] = False
    unprotected = dict(CDK_TEXT_ARTIFACTS, **{"synth/template.json": json.dumps(doc)})
    findings, _ = validate_artifacts(
        unprotected, [], profile="cdk-app", endpoint_auth=keyed
    )
    assert any(f.check == "auth_contract" and "GetMethod" in f.message for f in findings)

    # Explicit PUBLIC CDK output must be genuinely open, not just skip the key gate.
    doc = json.loads(CDK_TEMPLATE)
    for logical_id in ("Key", "Plan", "PlanKey"):
        del doc["Resources"][logical_id]
    doc["Resources"]["GetMethod"]["Properties"]["ApiKeyRequired"] = False
    del doc["Outputs"]["ApiKeyId"]
    public = dict(CDK_TEXT_ARTIFACTS, **{"synth/template.json": json.dumps(doc)})
    findings, _ = validate_artifacts(
        public,
        [],
        profile="cdk-app",
        endpoint_auth={"mode": "open", "source": "explicit_public"},
    )
    assert findings == []

    # disallowed resource in the SYNTHESIZED template
    doc = json.loads(CDK_TEMPLATE)
    doc["Resources"]["Bad"] = {"Type": "AWS::EC2::Instance", "Properties": {}}
    bad = dict(CDK_TEXT_ARTIFACTS, **{"synth/template.json": json.dumps(doc)})
    findings, _ = validate_artifacts(bad, [], profile="cdk-app")
    assert any(f.check == "service_allowlist" and f.path == "synth/template.json" for f in findings)

    # closure violation
    bad_pkg = json.dumps({"dependencies": {"aws-cdk-lib": "2", "left-pad": "1.0.0"}})
    findings, _ = validate_artifacts(
        dict(CDK_TEXT_ARTIFACTS, **{"package.json": bad_pkg}), [], profile="cdk-app"
    )
    assert any(f.check == "dependency_closure" and "left-pad" in f.message for f in findings)

    # missing ApiUrl in synth output
    doc = json.loads(CDK_TEMPLATE)
    doc["Outputs"] = {}
    findings, _ = validate_artifacts(
        dict(CDK_TEXT_ARTIFACTS, **{"synth/template.json": json.dumps(doc)}), [], profile="cdk-app"
    )
    assert any(f.check == "deploy_contract" for f in findings)

    # inline-cfn behavior unchanged (S8 parity)
    from tests.test_codegen import GOOD_ARTIFACTS

    findings, _ = validate_artifacts(dict(GOOD_ARTIFACTS), [], profile="inline-cfn")
    assert findings == []


# ------------------------------------------------------------------ synth packaging


def test_package_assembly_from_fixture(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "runner_synth", Path(__file__).parent.parent.parent / "runner" / "synth.py"
    )
    synth = importlib.util.module_from_spec(spec)
    sys.modules["runner_synth"] = spec.loader.exec_module(spec.loader and synth) or synth

    cdk_out = tmp_path / "cdk.out"
    asset_dir = cdk_out / "asset.deadbeef"
    asset_dir.mkdir(parents=True)
    (asset_dir / "index.py").write_text("def handler(e, c):\n    return {}\n")
    (cdk_out / "GeneratedApp.template.json").write_text(CDK_TEMPLATE)
    (cdk_out / "manifest.json").write_text(json.dumps({
        "artifacts": {
            "GeneratedApp": {
                "type": "aws:cloudformation:stack",
                "metadata": {
                    "/GeneratedApp/Fn/Code": [{
                        "type": "aws:cdk:asset",
                        "data": {
                            "path": "asset.deadbeef", "id": "deadbeef",
                            "packaging": "zip", "sourceHash": "deadbeef",
                            "s3BucketParameter": "AssetBucketParam",
                            "s3KeyParameter": "AssetKeyParam",
                            "artifactHashParameter": "AssetHashParam",
                        },
                    }]
                },
            }
        }
    }))

    template_body, assets = synth.package_assembly(cdk_out, tmp_path / "zips")
    assert json.loads(template_body)["Outputs"]["ApiUrl"]
    assert len(assets) == 1
    asset = assets[0]
    assert asset["bucket_parameter"] == "AssetBucketParam"
    assert asset["source_hash"] == "deadbeef"
    with zipfile.ZipFile(asset["zip_path"]) as archive:
        assert archive.namelist() == ["index.py"]
    sys.modules.pop("runner_synth", None)


# ------------------------------------------------------------------ profile plumbing


async def test_profile_resolution_and_gating(db_session, test_user, client_for, audit_db, monkeypatch):
    template = Template(
        name="CDK Default", status="active", category="custom",
        scaffolding={"codegen": {"artifact_profile": "cdk-app"}},
    )
    db_session.add(template)
    await db_session.commit()
    await db_session.refresh(template)
    project = await _project_with_docs(db_session, test_user, template)

    # internal provider + cdk-app (template default) → 409 with pointer copy
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 409
        assert "Model Controls" in r.json()["detail"]
        # unknown profile refused
        r = await client.post(
            f"/api/v1/projects/{project.id}/builds", json={"artifact_profile": "wasm"}
        )
        assert r.status_code == 409
        # explicit inline-cfn override works with the internal provider; this
        # fixture is unrelated to auth, so opt out explicitly before building.
        db_session.add(
            Spec(
                project_id=project.id,
                version=2,
                type="requirements",
                content="# requirements\nEndpoint authentication: PUBLIC\n",
                created_by=test_user.id,
            )
        )
        await db_session.commit()
        from tests.test_codegen import FakeProvider

        monkeypatch.setattr(runner, "get_provider", lambda name=None: FakeProvider())
        r = await client.post(
            f"/api/v1/projects/{project.id}/builds", json={"artifact_profile": "inline-cfn"}
        )
        assert r.status_code == 202
        body = r.json()
        assert body["artifact_profile"] == "inline-cfn"
        build = await _wait_terminal(db_session, uuid.UUID(body["id"]))
        assert build.status == "ready"


# ------------------------------------------------------------------ external cdk flow


async def test_external_cdk_build_offloads_and_gates(
    db_session, test_user, client_for, audit_db, monkeypatch, fake_artifact_store, fast_poll
):
    results = _cdk_results()
    fake = FakeWorkspaceProvider(
        observations=[ExternalStatus(started=True, files_seen=[], results=results)],
        artifacts=dict(CDK_TEXT_ARTIFACTS),
    )
    fake.prefix_root = lambda build_id, attempt: f"builds/{build_id}"
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)

    async with client_for(test_user) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/builds", json={"artifact_profile": "cdk-app"}
        )
        assert r.status_code == 202, r.text
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "ready", build.error
    assert build.manifest["artifact_profile"] == "cdk-app"
    assert build.manifest["synth"]["assets"][0]["source_hash"] == "abc123"

    rows = (
        (await db_session.execute(
            select(CodegenArtifact).where(CodegenArtifact.build_id == build.id)
        )).scalars().all()
    )
    assert all(r.content is None and r.s3_key for r in rows)  # S3 offload (R3)
    zip_rows = [r for r in rows if r.path.endswith(".zip")]
    assert zip_rows and zip_rows[0].language == "binary"

    # artifact API: text preview via resolver, binary download-only
    async with client_for(test_user) as client:
        r = await client.get(f"/api/v1/builds/{build.id}/artifacts/lib/app-stack.ts")
        assert r.json()["content"].startswith("import * as cdk")
        r = await client.get(f"/api/v1/builds/{build.id}/artifacts/synth/assets/abc123.zip")
        assert r.json()["binary"] is True and r.json()["content"] is None
        r = await client.get(
            f"/api/v1/builds/{build.id}/artifacts/synth/assets/abc123.zip?download=true"
        )
        assert r.status_code == 200 and r.content[:2] == b"PK"

        # bundle v2: repo layout — app files at root, specs alongside (R6)
        r = await client.get(f"/api/v1/builds/{build.id}/bundle")
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        names = archive.namelist()
        assert "package.json" in names and "lib/app-stack.ts" in names
        assert "synth/template.json" in names
        assert not any(n.startswith("generated/") for n in names)
        assert any(n.startswith(".kiro/specs/") for n in names)


# ------------------------------------------------------------------ deploy payload


async def test_resolve_payload_and_staging(
    db_session, test_user, audit_db, monkeypatch, fake_artifact_store
):
    project = await _project_with_docs(db_session, test_user)
    build = CodegenBuild(
        project_id=project.id, status="ready", provider="kiro", artifact_profile="cdk-app",
        manifest={"synth": {"assets": [{
            "id": "asset1", "path": "synth/assets/abc123.zip", "source_hash": "abc123",
            "bucket_parameter": "AssetBucketParam", "key_parameter": "AssetKeyParam",
            "hash_parameter": "AssetHashParam",
        }]}},
        created_by=test_user.id,
    )
    db_session.add(build)
    await db_session.flush()
    for path, content in CDK_TEXT_ARTIFACTS.items():
        key = storage.artifact_key(build.id, path)
        fake_artifact_store[key] = content.encode()
        db_session.add(CodegenArtifact(
            build_id=build.id, path=path, content=None, s3_key=key,
            content_hash="0" * 64, size_bytes=len(content),
        ))
    zip_key = storage.artifact_key(build.id, "synth/assets/abc123.zip")
    fake_artifact_store[zip_key] = ASSET_ZIP
    db_session.add(CodegenArtifact(
        build_id=build.id, path="synth/assets/abc123.zip", content=None, s3_key=zip_key,
        content_hash="0" * 64, size_bytes=len(ASSET_ZIP), language="binary",
    ))
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, build_id=build.id,
        status="pending", timeline=[], resources=[],
    )
    db_session.add(deployment)
    await db_session.commit()

    from app.services import deployment as dep_svc

    payload = await dep_svc._resolve_payload(db_session, deployment)
    assert json.loads(payload.template)["Outputs"]["ApiUrl"]
    assert payload.assets[0]["staged_key"] == "marshal-assets/abc123.zip"

    # staging: uploads via the assumed session + emits the CFN parameter triplet
    uploaded: list[tuple[str, str]] = []

    class FakeS3:
        def create_bucket(self, Bucket):  # noqa: N803
            uploaded.append(("bucket", Bucket))

        def put_object(self, Bucket, Key, Body):  # noqa: N803
            assert Body == ASSET_ZIP
            uploaded.append(("object", f"{Bucket}/{Key}"))

    class FakeSession:
        def client(self, name):
            return FakeS3()

    parameters = await dep_svc._stage_assets(FakeSession(), "123456789012", payload)
    assert ("bucket", "marshal-assets-123456789012") in uploaded
    assert ("object", "marshal-assets-123456789012/marshal-assets/abc123.zip") in uploaded
    values = {p["ParameterKey"]: p["ParameterValue"] for p in parameters}
    assert values["AssetBucketParam"] == "marshal-assets-123456789012"
    assert values["AssetKeyParam"] == "marshal-assets/abc123.zip||"
    assert values["AssetHashParam"] == "abc123"
