"""B19 web test console (web-test-console spec): signal, gate, staging,
teardown emptying, keyed interplay."""

import json

import pytest

from app.models import Deployment, Project
from app.services import deployment as deploy_svc
from app.services.codegen.validate import (
    requires_web_console,
    validate_artifacts,
)

pytestmark = pytest.mark.asyncio

# The live mortgage spec's prose — descriptive UI mentions must NOT opt in
MORTGAGE_PROSE = (
    "Guides applicants through pre-qualification and explains the decision "
    "factors in plain language. This service exposes the pre-qualification "
    "API the advisor UI calls."
)


def test_signal_detection_is_narrow():
    assert requires_web_console("The app SHALL provide a web test page.")
    assert requires_web_console("It provides a web interface for testing.")
    assert requires_web_console("Include a web test console for the API.")
    assert requires_web_console("The build SHALL include a web frontend.")
    # negatives — descriptive prose and adjacent phrasing
    assert not requires_web_console(MORTGAGE_PROSE)
    assert not requires_web_console("A separate web application consumes this API.")
    assert not requires_web_console("the UI team will build the interface later")
    assert not requires_web_console("")


CONSOLE_PAGE = (
    "<html><head><style>body{background:#111}</style></head><body>"
    "<h1>Test console</h1><input id='k' type='password'>"
    "<script>const BASE = location.pathname.replace(/\\/app\\/?$/, '');"
    "async function send(){const r=await fetch(BASE+'/notes',{headers:{'x-api-key':document.getElementById('k').value}});"
    "document.body.append(JSON.stringify(await r.json()));}</script>"
    "</body></html>" + "<!-- padding -->" * 30
)


def _console_template(
    *, bucket=True, blocked=True, fn=True, env=True, method=True,
    outputs=True, keyed_methods=False,
):
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
        "NotesMethod": {
            "Type": "AWS::ApiGateway::Method",
            "Properties": {
                "HttpMethod": "POST",
                "ApiKeyRequired": bool(keyed_methods),
                "Integration": {"Uri": {"Fn::Sub": "...Fn..."}},
            },
        },
    }
    if keyed_methods:
        resources["Key"] = {"Type": "AWS::ApiGateway::ApiKey", "Properties": {"Enabled": True}}
        resources["Plan"] = {
            "Type": "AWS::ApiGateway::UsagePlan",
            "Properties": {"ApiStages": [{"ApiId": {"Ref": "Api"}, "Stage": "prod"}]},
        }
        resources["PlanKey"] = {"Type": "AWS::ApiGateway::UsagePlanKey", "Properties": {}}
    if bucket:
        resources["WebConsoleBucket"] = {
            "Type": "AWS::S3::Bucket",
            "Properties": (
                {
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True, "BlockPublicPolicy": True,
                        "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
                    }
                }
                if blocked
                else {}
            ),
        }
    if fn:
        resources["WebConsoleFunction"] = {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "Code": {"ZipFile": "import boto3, os\ndef handler(e, c):\n    pass"},
                "Handler": "index.handler", "Runtime": "python3.12",
                "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                **(
                    {"Environment": {"Variables": {"WEB_BUCKET": {"Ref": "WebConsoleBucket"}}}}
                    if env
                    else {}
                ),
            },
        }
    if method:
        resources["AppMethod"] = {
            "Type": "AWS::ApiGateway::Method",
            "Properties": {
                "HttpMethod": "GET",
                "ApiKeyRequired": False,
                "Integration": {
                    "Type": "AWS_PROXY",
                    "Uri": {"Fn::Sub": "arn:...functions/${WebConsoleFunction.Arn}/invocations"},
                },
            },
        }
    out = {"ApiUrl": {"Value": "https://example"}}
    if keyed_methods:
        out["ApiKeyId"] = {"Value": {"Ref": "Key"}}  # G25: valid CloudFormation output shape
    if outputs:
        out["WebConsoleUrl"] = {"Value": "https://example/app"}
        out["WebBucketName"] = {"Value": {"Ref": "WebConsoleBucket"}}
    return json.dumps({"Resources": resources, "Outputs": out})


def _artifacts(template, page=CONSOLE_PAGE):
    artifacts = {
        "template.json": template,
        "src/handler.py": "def handler(event, context):\n    return {'statusCode': 200}\n",
        "README.md": "# x",
    }
    if page is not None:
        artifacts["web/index.html"] = page
    return artifacts


def _console_findings(template, page=CONSOLE_PAGE, require_api_key=False):
    findings, _ = validate_artifacts(
        _artifacts(template, page), [],
        require_api_key=require_api_key, require_web_console=True,
    )
    return [f for f in findings if f.check == "web_console"]


def test_conforming_console_passes():
    findings, _ = validate_artifacts(
        _artifacts(_console_template()), [], require_web_console=True
    )
    assert findings == []


def test_console_gate_matrix():
    assert any("PublicAccessBlock" in f.message for f in _console_findings(_console_template(blocked=False)))
    assert any("WebConsoleBucket" in f.message for f in _console_findings(_console_template(bucket=False, outputs=False)))
    assert any("WebConsoleFunction" in f.message and "required to serve" in f.message
               for f in _console_findings(_console_template(fn=False, method=False)))
    assert any("WEB_BUCKET" in f.message for f in _console_findings(_console_template(env=False)))
    assert any("integrates with" in f.message for f in _console_findings(_console_template(method=False)))
    assert any("Outputs.WebConsoleUrl" in f.message for f in _console_findings(_console_template(outputs=False)))
    assert any("was not generated" in f.message for f in _console_findings(_console_template(), page=None))
    assert any("real interactive document" in f.message
               for f in _console_findings(_console_template(), page="<html>tiny</html>"))
    # the live-drill class: root-relative paths drop the API GW stage prefix
    root_relative = CONSOLE_PAGE.replace("fetch(BASE+'/notes'", "fetch('/notes'")
    assert any("root-relative" in f.message
               for f in _console_findings(_console_template(), page=root_relative))


def test_keyed_console_interplay():
    """Keyed agent + console: /app stays exempt, agent methods stay keyed,
    the page must carry the paste-key wiring (R3.2 + R3.1)."""
    template = _console_template(keyed_methods=True)
    findings, _ = validate_artifacts(
        _artifacts(template), [], require_api_key=True, require_web_console=True
    )
    assert findings == []  # exempt console route does NOT trip auth_contract

    # page without x-api-key wiring fails when the spec is keyed
    bare_page = CONSOLE_PAGE.replace("x-api-key", "x-header")
    findings = _console_findings(template, page=bare_page, require_api_key=True)
    assert any("paste-key wiring" in f.message for f in findings)

    # and an UNPROTECTED agent method still fails auth_contract (exemption is
    # console-only)
    unkeyed_agent = json.loads(template)
    unkeyed_agent["Resources"]["NotesMethod"]["Properties"]["ApiKeyRequired"] = False
    findings, _ = validate_artifacts(
        _artifacts(json.dumps(unkeyed_agent)), [],
        require_api_key=True, require_web_console=True,
    )
    assert any(f.check == "auth_contract" and "NotesMethod" in f.message for f in findings)


def test_signal_off_leaves_gate_unchanged():
    findings, _ = validate_artifacts(
        _artifacts(_console_template(bucket=False, fn=False, method=False, outputs=False), page=None),
        [], require_web_console=False,
    )
    assert [f for f in findings if f.check == "web_console"] == []


# ------------------------------------------------- staging + emptying (R4)


async def test_stage_web_console(db_session, test_user):
    from app.models import CodegenArtifact, CodegenBuild

    project = Project(user_id=test_user.id, name="Console", status="deployed")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(project_id=project.id, status="ready", created_by=test_user.id)
    db_session.add(build)
    await db_session.flush()
    db_session.add(
        CodegenArtifact(
            build_id=build.id, path="web/index.html", content=CONSOLE_PAGE,
            content_hash="h", size_bytes=len(CONSOLE_PAGE),
        )
    )
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="active", build_id=build.id
    )
    db_session.add(deployment)
    await db_session.commit()

    puts: list[dict] = []

    class FakeS3:
        def put_object(self, **kwargs):
            puts.append(kwargs)

    class FakeSession:
        def client(self, name):
            assert name == "s3"
            return FakeS3()

    await deploy_svc._stage_web_console(
        db_session, deployment, FakeSession(), {"WebBucketName": "console-bkt"}
    )
    assert len(puts) == 1
    assert puts[0]["Bucket"] == "console-bkt" and puts[0]["Key"] == "index.html"
    assert puts[0]["ContentType"] == "text/html"
    assert b"Test console" in puts[0]["Body"]
    # no bucket output → no-op
    await deploy_svc._stage_web_console(db_session, deployment, FakeSession(), {})
    assert len(puts) == 1


async def test_empty_web_bucket():
    deleted: list[dict] = []

    class FakePaginator:
        def paginate(self, Bucket):  # noqa: N803
            yield {"Contents": [{"Key": "index.html"}, {"Key": "x.js"}]}
            yield {}

    class FakeS3:
        def get_paginator(self, name):
            return FakePaginator()

        def delete_objects(self, **kwargs):
            deleted.append(kwargs)

    class FakeSession:
        def client(self, name):
            return FakeS3()

    async def fake_outputs(cfn, stack_name):
        return {"WebBucketName": "console-bkt"}

    original = deploy_svc._stack_outputs
    deploy_svc._stack_outputs = fake_outputs
    try:
        await deploy_svc._empty_web_bucket(FakeSession(), object(), "stack-x")
    finally:
        deploy_svc._stack_outputs = original
    assert deleted and deleted[0]["Delete"]["Objects"] == [
        {"Key": "index.html"}, {"Key": "x.js"},
    ]
