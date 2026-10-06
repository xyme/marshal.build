"""S16-02 post-deploy smoke: manifest-derived route probing.

The gap this closes (S12 drill finding): the static gate proves the template
is ALLOWED; nothing proved the app behind it answers. These tests pin the
route derivation and the degraded/finding side effects without any AWS.
"""

import json

import pytest

from app.models import CodegenBuild, Deployment, Project
from app.services import deployment as svc

pytestmark = pytest.mark.asyncio


def _template(*route_keys: str) -> str:
    resources = {
        "Api": {"Type": "AWS::ApiGatewayV2::Api", "Properties": {}},
    }
    for index, key in enumerate(route_keys):
        resources[f"Route{index}"] = {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {"RouteKey": key},
        }
    return json.dumps({"Resources": resources})


async def test_derive_routes_default_only():
    probe, skipped = svc.derive_smoke_routes(_template("$default"))
    assert probe == ["/"] and skipped == []


async def test_derive_routes_gets_anys_and_skips():
    probe, skipped = svc.derive_smoke_routes(
        _template("GET /todos", "POST /todos", "ANY /health", "GET /items/{id}", "$default")
    )
    assert probe == ["/", "/todos", "/health"]
    # Non-GET and parameterized routes are RECORDED, not silently dropped
    assert skipped == ["POST /todos", "GET /items/{id}"]


async def test_derive_routes_tolerates_non_json_template():
    probe, skipped = svc.derive_smoke_routes("not json {{")
    assert probe == ["/"] and skipped == []


def _rest_template() -> str:
    """The REST shape the inline-cfn generator actually emits (5 Aug 2026
    live finding: only the HTTP-API shape was parsed, so REST templates
    smoke-passed on '/' alone with every declared route unexercised)."""
    return json.dumps({
        "Resources": {
            "RestApi": {"Type": "AWS::ApiGateway::RestApi", "Properties": {}},
            "PrequalifyResource": {
                "Type": "AWS::ApiGateway::Resource",
                "Properties": {
                    "ParentId": {"Fn::GetAtt": ["RestApi", "RootResourceId"]},
                    "PathPart": "prequalify",
                },
            },
            "ItemResource": {
                "Type": "AWS::ApiGateway::Resource",
                "Properties": {
                    "ParentId": {"Ref": "PrequalifyResource"},
                    "PathPart": "{id}",
                },
            },
            "PostMethod": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"HttpMethod": "POST", "ResourceId": {"Ref": "PrequalifyResource"}},
            },
            "GetMethod": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"HttpMethod": "GET", "ResourceId": {"Ref": "PrequalifyResource"}},
            },
            "GetItemMethod": {
                "Type": "AWS::ApiGateway::Method",
                "Properties": {"HttpMethod": "GET", "ResourceId": {"Ref": "ItemResource"}},
            },
        }
    })


async def test_derive_routes_rest_api_shape():
    probe, skipped = svc.derive_smoke_routes(_rest_template())
    assert probe == ["/", "/prequalify"]
    # Non-GET and parameterized REST routes recorded, not dropped
    assert "POST /prequalify" in skipped
    assert "GET /prequalify/{id}" in skipped


async def test_derive_routes_mixed_shapes_coexist():
    doc = json.loads(_rest_template())
    doc["Resources"]["V2Route"] = {
        "Type": "AWS::ApiGatewayV2::Route",
        "Properties": {"RouteKey": "GET /v2things"},
    }
    probe, skipped = svc.derive_smoke_routes(json.dumps(doc))
    assert set(probe) == {"/", "/v2things", "/prequalify"}
    assert "POST /prequalify" in skipped


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Route → status map; unknown paths 404 (API GW behavior)."""

    def __init__(self, statuses: dict[str, int]):
        self._statuses = statuses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
        for known, status in self._statuses.items():
            if path == known or url.endswith(known):
                return _FakeResponse(status)
        return _FakeResponse(404)


async def _deployment(db, user, *, with_build: bool = True):
    project = Project(user_id=user.id, name="Smoke", status="deployed")
    db.add(project)
    await db.commit()
    build = None
    if with_build:
        build = CodegenBuild(project_id=project.id, status="ready", manifest={"app_name": "x"})
        db.add(build)
        await db.commit()
    deployment = Deployment(
        project_id=project.id,
        user_id=user.id,
        build_id=build.id if build else None,
        status="active",
        health="healthy",
        app_url="https://abc123.execute-api.us-east-1.amazonaws.com",
    )
    db.add(deployment)
    await db.commit()
    await db.refresh(deployment)
    return deployment, build


async def test_smoke_pass_leaves_health_alone(db_session, admin_user, audit_db, monkeypatch):
    deployment, build = await _deployment(db_session, admin_user)
    template = _template("GET /todos", "$default")
    monkeypatch.setattr(
        svc.httpx, "AsyncClient",
        lambda **_kw: _FakeClient({"/": 200, "/todos": 200}),
    )

    await svc._post_deploy_smoke(db_session, deployment, template)

    await db_session.refresh(deployment)
    assert deployment.health == "healthy"
    assert deployment.timeline[-1]["phase"] == "smoke"
    await db_session.refresh(build)
    assert build.manifest["smoke"]["passed"] is True


async def test_smoke_failure_marks_degraded_and_writes_finding(
    db_session, admin_user, audit_db, monkeypatch
):
    deployment, build = await _deployment(db_session, admin_user)
    template = _template("GET /todos", "GET /broken")
    monkeypatch.setattr(
        svc.httpx, "AsyncClient",
        lambda **_kw: _FakeClient({"/": 200, "/todos": 200, "/broken": 502}),
    )

    await svc._post_deploy_smoke(db_session, deployment, template)

    await db_session.refresh(deployment)
    assert deployment.health == "degraded"
    await db_session.refresh(build)
    report = build.manifest["smoke"]
    assert report["passed"] is False
    by_path = {p["path"]: p for p in report["probed"]}
    assert by_path["/broken"]["ok"] is False and by_path["/todos"]["ok"] is True


async def test_smoke_404_on_declared_route_fails(db_session, admin_user, audit_db, monkeypatch):
    """A declared route that 404s is the exact static-gate/runtime gap."""
    deployment, _build = await _deployment(db_session, admin_user, with_build=False)
    template = _template("GET /missing")
    monkeypatch.setattr(
        svc.httpx, "AsyncClient", lambda **_kw: _FakeClient({"/": 200})
    )

    await svc._post_deploy_smoke(db_session, deployment, template)

    await db_session.refresh(deployment)
    assert deployment.health == "degraded"


async def test_smoke_auth_refusal_passes(db_session, admin_user, audit_db, monkeypatch):
    """401/403 mean the route is ALIVE (auth'd) — not a runtime failure."""
    deployment, _build = await _deployment(db_session, admin_user, with_build=False)
    template = _template("GET /secure")
    monkeypatch.setattr(
        svc.httpx, "AsyncClient",
        lambda **_kw: _FakeClient({"/": 200, "/secure": 403}),
    )

    await svc._post_deploy_smoke(db_session, deployment, template)

    await db_session.refresh(deployment)
    assert deployment.health == "healthy"
