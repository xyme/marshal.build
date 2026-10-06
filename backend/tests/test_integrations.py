"""B10 webhooks + B11 chat-ops (integration-wave spec R3/R4)."""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Alert, PlatformSettings, User, WebhookDelivery, WebhookEndpoint
from app.services import chatops as chatops_svc
from app.services import webhooks as webhooks_svc

pytestmark = pytest.mark.asyncio


async def _admin(db) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:10]}",
        email=f"admin-{uuid.uuid4().hex[:6]}@marshal.demo",
        role="admin", persona="power",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _fake_http(monkeypatch, *, status_code=200, raises=None, capture=None):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None, json=None):
            if capture is not None:
                capture.append({"url": url, "content": content, "headers": headers, "json": json})
            if raises:
                raise raises
            return FakeResponse(status_code)

    monkeypatch.setattr(webhooks_svc.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(chatops_svc.httpx, "AsyncClient", FakeClient)


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch):
    """Hermetic: Secrets Manager never touched; fixed signing secret."""
    monkeypatch.setattr(webhooks_svc, "_store_secret", lambda *a, **k: None)

    async def fixed_secret(endpoint_id):
        return "test-signing-secret"

    monkeypatch.setattr(webhooks_svc, "_secret_for", fixed_secret)
    monkeypatch.setattr(chatops_svc, "_store_url", lambda url: None)

    async def fixed_url():
        return "https://hooks.slack.example/T000/B000"

    monkeypatch.setattr(chatops_svc, "_url", fixed_url)
    chatops_svc.invalidate_cache()
    chatops_svc._recent_keys.clear()
    yield
    chatops_svc.invalidate_cache()
    chatops_svc._recent_keys.clear()


# ------------------------------------------------------- registration (R3.1)


async def test_webhook_registration_rules(db_session, client_for, audit_db):
    admin = await _admin(db_session)
    async with client_for(admin) as client:
        r = await client.post(
            "/api/v1/admin/integrations/webhooks",
            json={"url": "http://insecure.example", "event_types": ["build_ready"]},
        )
        assert r.status_code == 422 and "https" in r.json()["detail"]
        r = await client.post(
            "/api/v1/admin/integrations/webhooks",
            json={"url": "https://ok.example/hook", "event_types": ["not_an_event"]},
        )
        assert r.status_code == 422
        r = await client.post(
            "/api/v1/admin/integrations/webhooks",
            json={"url": "https://ok.example/hook", "event_types": ["build_ready", "deploy_succeeded"]},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["secret"].startswith("whsec_")  # shown once (R3.1)
        # list responses never carry the secret
        r = await client.get("/api/v1/admin/integrations/webhooks")
        assert "secret" not in json.dumps(r.json())


# ------------------------------------------------- dispatch + dedupe (R3.2)


async def _endpoint(db, events, active=True) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(url="https://consumer.example/hook", event_types=events, active=active)
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


async def test_dispatch_subscription_and_dedupe(db_session, audit_db, monkeypatch):
    captured: list = []
    _fake_http(monkeypatch, capture=captured)
    subscribed = await _endpoint(db_session, ["build_ready"])
    await _endpoint(db_session, ["risk_decided"])  # different event
    await _endpoint(db_session, ["build_ready"], active=False)  # inactive

    await webhooks_svc.dispatch(
        "build_ready", title="Build ready", body="x", link="/projects/1",
        dedupe_key="build_ready:abc",
    )
    rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1 and rows[0].endpoint_id == subscribed.id
    assert rows[0].status == "delivered"
    # the notify_many fan-out repeats the dedupe key → no second delivery
    await webhooks_svc.dispatch(
        "build_ready", title="Build ready", body="x", link="/projects/1",
        dedupe_key="build_ready:abc",
    )
    rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    # non-allowlisted events never dispatch
    await webhooks_svc.dispatch("comment_reply", title="x", dedupe_key="k")
    assert len((await db_session.execute(select(WebhookDelivery))).scalars().all()) == 1


async def test_signature_is_verifiable(db_session, audit_db, monkeypatch):
    captured: list = []
    _fake_http(monkeypatch, capture=captured)
    await _endpoint(db_session, ["deploy_succeeded"])
    await webhooks_svc.dispatch("deploy_succeeded", title="Deployed", dedupe_key="d:1")
    assert captured
    sent = captured[0]
    signature = sent["headers"]["x-marshal-signature"]
    t_part, v_part = signature.split(",")
    timestamp = int(t_part.split("=")[1])
    expected = hmac.new(
        b"test-signing-secret", f"{timestamp}.{sent['content']}".encode(), hashlib.sha256
    ).hexdigest()
    assert v_part == f"v1={expected}"
    payload = json.loads(sent["content"])
    assert payload["event"] == "deploy_succeeded" and payload["delivery_id"]
    assert "api_key" not in sent["content"]  # payloads never carry secrets


# ------------------------------------------------- retries + dead (R3.3)


async def test_retry_backoff_and_dead_alert(db_session, audit_db, monkeypatch):
    _fake_http(monkeypatch, status_code=500)
    endpoint = await _endpoint(db_session, ["build_failed"])
    await webhooks_svc.dispatch("build_failed", title="failed", dedupe_key="bf:1")
    delivery = (await db_session.execute(select(WebhookDelivery))).scalars().first()
    assert delivery.status == "failed" and delivery.attempts == 1
    assert delivery.next_attempt_at is not None

    # sweep to death: force-due each round
    for _ in range(webhooks_svc.MAX_ATTEMPTS):
        delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await webhooks_svc.delivery_tick()
        await db_session.refresh(delivery)
    assert delivery.status == "dead"
    assert delivery.attempts == webhooks_svc.MAX_ATTEMPTS
    alert = (
        await db_session.execute(select(Alert).where(Alert.kind == "webhook"))
    ).scalars().first()
    assert alert is not None and endpoint.url in alert.message


async def test_delivery_recovers_on_retry(db_session, audit_db, monkeypatch):
    _fake_http(monkeypatch, status_code=503)
    await _endpoint(db_session, ["deployment_expired"])
    await webhooks_svc.dispatch("deployment_expired", title="expired", dedupe_key="de:1")
    delivery = (await db_session.execute(select(WebhookDelivery))).scalars().first()
    assert delivery.status == "failed"
    _fake_http(monkeypatch, status_code=200)
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await webhooks_svc.delivery_tick()
    await db_session.refresh(delivery)
    assert delivery.status == "delivered" and delivery.delivered_at is not None


# ------------------------------------------------------- chat-ops (R4)


async def _configure_chatops(db, events):
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
    row.chat_ops = {"enabled": True, "provider": "slack", "events": events}
    await db.commit()
    chatops_svc.invalidate_cache()


async def test_chatops_routing_and_dedupe(db_session, audit_db, monkeypatch):
    captured: list = []
    _fake_http(monkeypatch, capture=captured)
    await _configure_chatops(db_session, ["risk_decided"])

    await chatops_svc.post("risk_decided", title="Approved", body="b", link="/reviews", dedupe_key="r:1")
    assert len(captured) == 1
    assert "Approved" in captured[0]["json"]["text"]
    from app.core.config import get_settings

    # absolute link, rooted at the installation's APP_BASE_URL
    assert f"{get_settings().app_base_url}/reviews" in captured[0]["json"]["text"]
    # same dedupe key → one post (notify_many fan-out)
    await chatops_svc.post("risk_decided", title="Approved", body="b", link="/reviews", dedupe_key="r:1")
    assert len(captured) == 1
    # unrouted event → silent skip
    await chatops_svc.post("build_ready", title="x", dedupe_key="b:1")
    assert len(captured) == 1


async def test_chatops_silent_failure(db_session, audit_db, monkeypatch):
    import httpx

    _fake_http(monkeypatch, raises=httpx.ConnectError("down"))
    await _configure_chatops(db_session, ["build_ready"])
    # must not raise (R4.2)
    await chatops_svc.post("build_ready", title="x", dedupe_key="b:2")


async def test_chatops_config_api(db_session, client_for, audit_db):
    admin = await _admin(db_session)
    async with client_for(admin) as client:
        r = await client.put(
            "/api/v1/admin/integrations/chat-ops",
            json={"enabled": True, "provider": "slack", "events": ["risk_decided"], "url": "https://hooks.slack.example/x"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True and body["has_url"] is True
        assert "hooks.slack.example" not in json.dumps({k: v for k, v in body.items() if k != "has_url"})
        r = await client.put(
            "/api/v1/admin/integrations/chat-ops", json={"events": ["nope"]}
        )
        assert r.status_code == 422


# --------------------------------------------- notify() hook (R3.2/R4.2)


async def test_notify_hook_dispatches_integrations(db_session, test_user, audit_db, monkeypatch):
    from app.services import notifications as notif

    seen: list = []

    async def fake_dispatch(event, **kwargs):
        seen.append(("webhook", event, kwargs.get("dedupe_key")))

    async def fake_post(event, **kwargs):
        seen.append(("chatops", event, kwargs.get("dedupe_key")))

    monkeypatch.setattr(webhooks_svc, "dispatch", fake_dispatch)
    monkeypatch.setattr(chatops_svc, "post", fake_post)

    # mute BOTH user channels — integrations must still fire (pre-pref hook)
    test_user.notification_prefs = {"build_ready": {"in_app": False, "email": False}}
    await db_session.commit()
    row = await notif.notify(
        db_session, test_user, type="build_ready", title="t", dedupe_key="k1"
    )
    assert row is None  # user channels muted
    import asyncio

    await asyncio.sleep(0.05)  # let the fire-and-forget tasks run
    assert ("webhook", "build_ready", "k1") in seen
    assert ("chatops", "build_ready", "k1") in seen


# ------------------------------------------------------- C0 connectors
# Spec: .kiro/specs/external-import-connectors — registry + reachability
# only; credentials write-only in Secrets Manager (stubbed here).


def _stub_connector_secrets(monkeypatch, stored: dict):
    from app.services import connectors as connectors_svc

    monkeypatch.setattr(
        connectors_svc, "_store_credential", lambda slug, cred: stored.__setitem__(slug, cred)
    )
    monkeypatch.setattr(
        connectors_svc, "_delete_credential", lambda slug: stored.pop(slug, None)
    )

    async def _cred(slug, **_kwargs):  # accepts the deploy path's degrade=False
        return stored.get(slug)

    monkeypatch.setattr(connectors_svc, "_credential_for", _cred)


def _fake_probe_http(monkeypatch, *, status_code=200, raises=None, capture=None):
    import httpx as _httpx

    from app.services import connectors as connectors_svc

    class FakeResp:
        def __init__(self, code, payload=None, headers=None):
            self.status_code = code
            self.text = "" if code < 400 else "refused"
            self._payload = payload
            self.headers = headers or {"content-type": "application/json"}

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def head(self, url, headers=None):
            if capture is not None:
                capture.append({"url": url, "headers": headers})
            if raises:
                raise raises
            return FakeResp(status_code)

        async def get(self, url, headers=None):
            return await self.head(url, headers=headers)

        async def post(self, url, headers=None, json=None):
            # Minimal MCP-shaped answers so mcp_server probes complete their
            # C2 handshake; dedicated handshake tests use _fake_mcp_http.
            if raises:
                raise raises
            method = (json or {}).get("method")
            if method == "initialize":
                return FakeResp(
                    200,
                    {"jsonrpc": "2.0", "id": 1,
                     "result": {"protocolVersion": "2025-03-26",
                                "serverInfo": {"name": "fake", "version": "0"}}},
                )
            if method == "tools/list":
                return FakeResp(
                    200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
                )
            return FakeResp(202, {})

    monkeypatch.setattr(connectors_svc.httpx, "AsyncClient", FakeClient)
    return _httpx


async def test_connector_crud_and_credential_custody(
    db_session, client_for, monkeypatch, audit_db
):
    stored: dict = {}
    _stub_connector_secrets(monkeypatch, stored)
    admin = await _admin(db_session)
    async with client_for(admin) as ac:
        created = await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "claims-db",
                "name": "Claims warehouse",
                "type": "data_source",
                "base_url": "https://warehouse.example.com/api",
                "credential": "sekret-token-value",
            },
        )
        assert created.status_code == 201, created.text
        # AC-4: the credential value appears nowhere in any response
        assert "sekret-token-value" not in created.text
        assert created.json()["has_credential"] is True
        assert stored["claims-db"] == "sekret-token-value"

        listing = await ac.get("/api/v1/admin/integrations/connectors")
        assert "sekret-token-value" not in listing.text
        assert [c["slug"] for c in listing.json()["items"]] == ["claims-db"]

        # duplicate slug refused
        dup = await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "claims-db", "name": "Dup", "type": "http_api",
                "base_url": "https://dup.example.com",
            },
        )
        assert dup.status_code == 422

        # http:// refused by service validation
        bad = await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "plain", "name": "Plain", "type": "http_api",
                "base_url": "http://insecure.example.com",
            },
        )
        assert bad.status_code == 422

        updated = await ac.put(
            "/api/v1/admin/integrations/connectors/claims-db",
            json={"name": "Claims warehouse (prod)", "clear_credential": True},
        )
        assert updated.status_code == 200
        assert updated.json()["has_credential"] is False
        assert "claims-db" not in stored

        toggled = await ac.put(
            "/api/v1/admin/integrations/connectors/claims-db/status",
            json={"active": False},
        )
        assert toggled.json()["active"] is False

        deleted = await ac.delete("/api/v1/admin/integrations/connectors/claims-db")
        assert deleted.status_code == 200
        assert (await ac.get("/api/v1/admin/integrations/connectors")).json()["items"] == []


async def test_connector_probe_stores_outcomes(db_session, client_for, monkeypatch, audit_db):
    stored: dict = {"mcp-hub": "tok"}
    _stub_connector_secrets(monkeypatch, stored)
    capture: list = []
    _fake_probe_http(monkeypatch, status_code=200, capture=capture)
    admin = await _admin(db_session)
    async with client_for(admin) as ac:
        await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "mcp-hub", "name": "MCP hub", "type": "mcp_server",
                "base_url": "https://mcp.example.com", "credential": "tok",
                "auth_header": "X-Api-Key",
            },
        )
        probe = await ac.post("/api/v1/admin/integrations/connectors/mcp-hub/probe")
        assert probe.status_code == 200
        assert probe.json()["ok"] is True and probe.json()["status"] == 200
        # credential header attached, value never in the response
        assert capture[0]["headers"]["X-Api-Key"] == "tok"
        assert "tok" not in probe.text or probe.json().get("status") == 200

        listing = await ac.get("/api/v1/admin/integrations/connectors")
        assert listing.json()["items"][0]["last_probe"]["ok"] is True


async def test_connector_probe_failure_is_stored_not_raised(
    db_session, client_for, monkeypatch, audit_db
):
    import httpx as _httpx

    _stub_connector_secrets(monkeypatch, {})
    _fake_probe_http(monkeypatch, raises=_httpx.ConnectError("boom"))
    admin = await _admin(db_session)
    async with client_for(admin) as ac:
        await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "down", "name": "Down", "type": "http_api",
                "base_url": "https://down.example.com",
            },
        )
        probe = await ac.post("/api/v1/admin/integrations/connectors/down/probe")
        assert probe.status_code == 200  # AC-5: failure stored, not thrown
        body = probe.json()
        assert body["ok"] is False and body["error_class"] == "ConnectError"


async def test_connector_viewer_reads_but_never_mutates(
    db_session, business_user, client_for, monkeypatch, audit_db
):
    _stub_connector_secrets(monkeypatch, {})
    business_user.admin_readonly = True
    await db_session.commit()
    async with client_for(business_user) as ac:
        assert (await ac.get("/api/v1/admin/integrations/connectors")).status_code == 200
        create = await ac.post(
            "/api/v1/admin/integrations/connectors",
            json={
                "slug": "x1", "name": "X", "type": "http_api",
                "base_url": "https://x.example.com",
            },
        )
        assert create.status_code == 403  # AC-6
        assert create.json()["detail"]["code"] == "admin_read_only"
        probe = await ac.post("/api/v1/admin/integrations/connectors/x1/probe")
        assert probe.status_code == 403


# ------------------------------------------ C1 copy custody (deployer + preflight)


async def _registry_with(db_session, *entries):
    from app.models import PlatformSettings

    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.connectors = [
        {
            "slug": slug,
            "name": slug,
            "type": "http_api",
            "base_url": f"https://{slug}.example.com",
            "auth_header": "X-Api-Key",
            "active": active,
            "has_credential": True,
        }
        for slug, active in entries
    ]
    await db_session.commit()
    return row


async def test_ensure_connector_secrets_copies_composite_payload(
    db_session, test_user, monkeypatch
):
    import json as _json

    from app.models import CodegenBuild, Deployment, Project
    from app.services import connectors as connectors_svc
    from app.services import deployment as deploy_svc

    await _registry_with(db_session, ("support-api", True))
    project = Project(user_id=test_user.id, name="C1", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="ready",
        created_by=test_user.id,
        manifest={"declared_connectors": ["support-api"]},
    )
    db_session.add(build)
    await db_session.flush()
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="deploying", build_id=build.id
    )
    db_session.add(deployment)
    await db_session.commit()

    async def fake_credential(slug):
        return "sekret-value"

    monkeypatch.setattr(connectors_svc, "credential_for", fake_credential)

    created: list[dict] = []

    class FakeSM:
        def create_secret(self, **kwargs):
            created.append(kwargs)

        def put_secret_value(self, **kwargs):  # pragma: no cover — create path used
            created.append(kwargs)

    class FakeSession:
        def client(self, name):
            assert name == "secretsmanager"
            return FakeSM()

    count = await deploy_svc._ensure_connector_secrets(db_session, deployment, FakeSession())
    assert count == 1
    assert created[0]["Name"] == "marshal/agent-connectors/support-api"
    payload = _json.loads(created[0]["SecretString"])
    assert payload == {
        "base_url": "https://support-api.example.com",
        "header": "X-Api-Key",
        "value": "sekret-value",
    }

    # Inactive connector fails CLOSED with a named error
    await _registry_with(db_session, ("support-api", False))
    with pytest.raises(ValueError, match="not registered and active"):
        await deploy_svc._ensure_connector_secrets(db_session, deployment, FakeSession())

    # No declaration → no-op, no client calls
    build.manifest = {"declared_connectors": []}
    await db_session.commit()

    class ExplodingSession:
        def client(self, name):  # pragma: no cover — must not be called
            raise AssertionError("no secrets client for zero declarations")

    assert (
        await deploy_svc._ensure_connector_secrets(db_session, deployment, ExplodingSession())
        == 0
    )


async def test_deploy_preflight_connector_refusals(
    db_session, test_user, client_for, audit_db
):
    from app.models import CodegenBuild, PlatformSettings, Project
    from app.services import platform_settings as settings_svc

    await _registry_with(db_session, ("support-api", True))
    row = await db_session.get(PlatformSettings, 1)
    row.feature_flags = {**(row.feature_flags or {}), "connectors_enabled": False}
    await db_session.commit()
    settings_svc.invalidate_cache()

    project = Project(user_id=test_user.id, name="C1-preflight", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="ready",
        created_by=test_user.id,
        manifest={
            "declared_connectors": ["support-api"],
            "endpoint_auth": {"mode": "key_required", "source": "default"},
        },
    )
    db_session.add(build)
    await db_session.commit()

    async with client_for(test_user) as ac:
        # Flag off + declared → named 422 BEFORE scoring/lease spend
        r = await ac.post(
            f"/api/v1/projects/{project.id}/deploy", json={"build_id": str(build.id)}
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "connectors_disabled"

        # Flag on but the declared slug is inactive → connector_unavailable
        row.feature_flags = {**(row.feature_flags or {}), "connectors_enabled": True}
        await db_session.commit()
        await _registry_with(db_session, ("support-api", False))
        settings_svc.invalidate_cache()
        r = await ac.post(
            f"/api/v1/projects/{project.id}/deploy", json={"build_id": str(build.id)}
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "connector_unavailable"

        # C3 v1: an MCP tool source that is registered+active but no longer
        # mcp_server-typed refuses with the type-naming message
        await _registry_with(db_session, ("support-api", True))
        build.manifest = {
            **build.manifest,
            "mcp_tool_connectors": ["support-api"],  # registry says http_api
        }
        await db_session.commit()
        settings_svc.invalidate_cache()
        r = await ac.post(
            f"/api/v1/projects/{project.id}/deploy", json={"build_id": str(build.id)}
        )
        assert r.status_code == 422
        assert "no longer mcp_server-typed" in r.json()["detail"]["detail"]
    settings_svc.invalidate_cache()


# --------------------------------------------- C2: MCP handshake + grounding


def _fake_mcp_http(monkeypatch, *, tools=None, init_error=None, non_json=False):
    from app.services import connectors as connectors_svc

    class FakeResp:
        def __init__(self, code=200, payload=None, headers=None, text=""):
            self.status_code = code
            self._payload = payload
            self.headers = headers or {"content-type": "application/json"}
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("no json body")
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def head(self, url, headers=None):
            return FakeResp(200, {})

        async def get(self, url, headers=None):
            return FakeResp(200, {})

        async def post(self, url, headers=None, json=None):
            if non_json:
                return FakeResp(200, None, {"content-type": "text/html"}, "<html>")
            method = (json or {}).get("method")
            if method == "initialize":
                if init_error:
                    return FakeResp(
                        200,
                        {"jsonrpc": "2.0", "id": 1,
                         "error": {"code": -1, "message": init_error}},
                    )
                return FakeResp(
                    200,
                    {"jsonrpc": "2.0", "id": 1,
                     "result": {"protocolVersion": "2025-03-26",
                                "serverInfo": {"name": "docs-mcp", "version": "1.2"}}},
                    {"content-type": "application/json", "mcp-session-id": "sess-1"},
                )
            if method == "notifications/initialized":
                return FakeResp(202, {})
            if method == "tools/list":
                return FakeResp(
                    200,
                    {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools or []}},
                )
            return FakeResp(400, {})

    monkeypatch.setattr(connectors_svc.httpx, "AsyncClient", FakeClient)


async def _mcp_entry(db_session, slug="docs-mcp"):
    from app.models import PlatformSettings

    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.connectors = [
        {
            "slug": slug, "name": slug, "type": "mcp_server",
            "base_url": f"https://{slug}.example.com", "active": True,
            "has_credential": False,
        }
    ]
    await db_session.commit()
    return row


async def test_mcp_handshake_enriches_probe(db_session, monkeypatch):
    from app.services import connectors as connectors_svc

    _stub_connector_secrets(monkeypatch, {})  # no live Secrets Manager (fork-safe CI)
    await _mcp_entry(db_session)
    _fake_mcp_http(
        monkeypatch,
        tools=[
            {"name": "search_docs", "description": "Full-text search"},
            {"name": "fetch_page", "description": "Fetch one page"},
        ],
    )
    await connectors_svc.probe(db_session, "docs-mcp")
    from app.models import PlatformSettings

    row = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(row)
    entry = row.connectors[0]
    assert entry["mcp"]["ok"] is True
    assert entry["mcp"]["server"] == "docs-mcp 1.2"
    assert [t["name"] for t in entry["mcp"]["tools"]] == ["search_docs", "fetch_page"]
    # public_view carries the metadata (and still no credential material)
    view = connectors_svc.public_view(entry)
    assert view["mcp"]["tools"][0]["name"] == "search_docs"


async def test_mcp_handshake_failures_stored_honestly(db_session, monkeypatch):
    from app.models import PlatformSettings
    from app.services import connectors as connectors_svc

    _stub_connector_secrets(monkeypatch, {})  # no live Secrets Manager (fork-safe CI)
    await _mcp_entry(db_session)
    _fake_mcp_http(monkeypatch, non_json=True)
    await connectors_svc.probe(db_session, "docs-mcp")
    row = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(row)
    assert row.connectors[0]["mcp"]["ok"] is False
    assert "ValueError" in row.connectors[0]["mcp"]["error"]

    _fake_mcp_http(monkeypatch, init_error="unsupported client")
    await connectors_svc.probe(db_session, "docs-mcp")
    row = await db_session.get(PlatformSettings, 1)
    await db_session.refresh(row)
    assert "initialize refused" in row.connectors[0]["mcp"]["error"]


async def test_connector_grounding_block_flag_gated(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.services import connectors as connectors_svc
    from app.services import platform_settings as settings_svc

    row = await _mcp_entry(db_session)
    entries = [dict(e) for e in row.connectors]
    entries[0]["mcp"] = {"ok": True, "tools": [{"name": "search_docs", "description": ""}]}
    entries.append(
        {"slug": "old-api", "name": "old", "type": "http_api",
         "base_url": "https://old.example.com", "active": False}
    )
    row.connectors = entries
    await db_session.commit()

    flags = {"connectors_enabled": False}

    async def fake_controls():
        return SimpleNamespace(feature_flags=dict(flags))

    monkeypatch.setattr(settings_svc, "get_controls", fake_controls)

    assert await connectors_svc.connector_grounding_block(db_session) == ""

    flags["connectors_enabled"] = True
    block = await connectors_svc.connector_grounding_block(db_session)
    assert "SHALL use connector <slug>" in block
    assert "docs-mcp (mcp_server)" in block
    assert "search_docs" in block
    assert "old-api" not in block  # inactive entries never ground
    assert len(block) <= connectors_svc.GROUNDING_MAX_CHARS


# --------------------------------- composable agents deploy/teardown (13.5R)


async def test_deploy_preflight_refuses_undeployed_dependency(
    db_session, test_user, client_for, audit_db
):
    from app.models import Project

    dep = Project(user_id=test_user.id, name="Dep Agent", status="spec_complete")
    caller = Project(user_id=test_user.id, name="Caller Agent", status="spec_complete")
    db_session.add_all([dep, caller])
    await db_session.flush()
    caller.composition = {
        "dependencies": [
            {"slug": "dep-agent-x", "project_id": str(dep.id), "name": dep.name}
        ],
        "orchestration": None,
        "depth": 1,
    }
    await db_session.commit()

    async with client_for(test_user) as ac:
        r = await ac.post(f"/api/v1/projects/{caller.id}/deploy", json={})
        assert r.status_code == 422
        body = r.json()["detail"]
        assert body["code"] == "composition_dependency_unavailable"
        assert "dep-agent-x" in body["detail"]


async def test_teardown_refuses_with_live_dependents(db_session, test_user):
    from app.models import Deployment, Project
    from app.services import deployment as deploy_svc

    dep = Project(user_id=test_user.id, name="Shared Service", status="deployed")
    caller = Project(user_id=test_user.id, name="Live Caller", status="deployed")
    db_session.add_all([dep, caller])
    await db_session.flush()
    caller.composition = {
        "dependencies": [
            {"slug": "shared-service-x", "project_id": str(dep.id), "name": dep.name}
        ],
        "orchestration": None,
        "depth": 1,
    }
    dep_deployment = Deployment(
        project_id=dep.id, user_id=test_user.id, status="active", app_url="https://x"
    )
    caller_deployment = Deployment(
        project_id=caller.id, user_id=test_user.id, status="active", app_url="https://y"
    )
    db_session.add_all([dep_deployment, caller_deployment])
    await db_session.commit()

    # Default: refused, dependents NAMED
    with pytest.raises(deploy_svc.DependentsBlockTeardown) as exc:
        await deploy_svc.start_teardown(db_session, dep_deployment)
    assert exc.value.dependents == ["Live Caller"]

    # Caller torn down → dependency teardown proceeds
    caller_deployment.status = "torn_down"
    await db_session.commit()
    await deploy_svc.start_teardown(db_session, dep_deployment)
    await db_session.refresh(dep_deployment)
    assert dep_deployment.status == "tearing_down"

    # force=True (the TTL sweeper) bypasses even with live dependents
    caller_deployment.status = "active"
    dep_deployment.status = "active"
    await db_session.commit()
    await deploy_svc.start_teardown(db_session, dep_deployment, force=True)
    await db_session.refresh(dep_deployment)
    assert dep_deployment.status == "tearing_down"


async def test_ensure_dependency_secrets_cross_lease_copy(
    db_session, test_user, monkeypatch
):
    import json as _json

    from app.models import Deployment, Lease, Project
    from app.services import deployment as deploy_svc

    dep = Project(user_id=test_user.id, name="Dep Agent", status="deployed")
    caller = Project(user_id=test_user.id, name="Caller", status="spec_complete")
    db_session.add_all([dep, caller])
    await db_session.flush()
    caller.composition = {
        "dependencies": [
            {"slug": "dep-agent-1a2b3c4d", "project_id": str(dep.id), "name": dep.name}
        ],
        "orchestration": None,
        "depth": 1,
    }
    dep_lease = Lease(provider="isb", status="active", project_id=dep.id,
                      user_id=test_user.id, aws_account_id="111122223333",
                      external_lease_id="lease-b")
    db_session.add(dep_lease)
    await db_session.flush()
    dep_deployment = Deployment(
        project_id=dep.id, user_id=test_user.id, status="active",
        app_url="https://dep.example.com/prod", stack_name="marshal-dep",
        lease_id=dep_lease.id,
    )
    caller_deployment = Deployment(
        project_id=caller.id, user_id=test_user.id, status="deploying",
    )
    db_session.add_all([dep_deployment, caller_deployment])
    await db_session.commit()

    async def fake_outputs(cfn, stack_name):
        assert stack_name == "marshal-dep"
        return {"ApiUrl": "https://dep.example.com/prod", "ApiKeyId": "k-1"}

    async def fake_key(session, outputs):
        return "dep-key-value"

    class FakeDepSession:
        def client(self, name):
            return object()

    class FakeProvider:
        name = "isb"

        def deployment_session(self, info):
            assert info.aws_account_id == "111122223333"
            return FakeDepSession()

    monkeypatch.setattr(deploy_svc, "_stack_outputs", fake_outputs)
    monkeypatch.setattr(deploy_svc, "_fetch_api_key", fake_key)
    monkeypatch.setattr(deploy_svc, "provider_for_lease", lambda lease: FakeProvider())

    created: list[dict] = []

    class FakeSM:
        def create_secret(self, **kwargs):
            created.append(kwargs)

        def put_secret_value(self, **kwargs):  # pragma: no cover
            created.append(kwargs)

    class CallerSession:
        def client(self, name):
            assert name == "secretsmanager"
            return FakeSM()

    wired = await deploy_svc._ensure_dependency_secrets(
        db_session, caller_deployment, CallerSession()
    )
    assert wired == 1
    assert created[0]["Name"] == "marshal/agent-dependencies/dep-agent-1a2b3c4d"
    payload = _json.loads(created[0]["SecretString"])
    assert payload == {
        "base_url": "https://dep.example.com/prod",
        "header": "x-api-key",
        "value": "dep-key-value",
    }

    # Dependency torn down mid-deploy → fail-closed with a named error
    dep_deployment.status = "torn_down"
    await db_session.commit()
    with pytest.raises(ValueError, match="no active deployment"):
        await deploy_svc._ensure_dependency_secrets(
            db_session, caller_deployment, CallerSession()
        )


# ------------------------------------------- connector credential store (B7)
# A fork / fresh install with no AWS credentials (or no region) must probe
# connectors WITHOUT a credential instead of 500ing: NoCredentialsError and the
# other BotoCoreError shapes are "no credential", logged once per process.


async def test_credential_for_degrades_without_aws_credentials(monkeypatch, caplog):
    import logging

    from botocore.exceptions import EndpointConnectionError, NoCredentialsError

    from app.services import connectors as connectors_svc

    class NoCredsSM:
        calls = 0

        def get_secret_value(self, **kwargs):
            NoCredsSM.calls += 1
            raise NoCredentialsError()

    monkeypatch.setattr(connectors_svc, "_secrets", lambda: NoCredsSM())
    monkeypatch.setattr(connectors_svc, "_credential_backend_warned", False)
    with caplog.at_level(logging.WARNING, logger="marshal.connectors"):
        assert await connectors_svc._credential_for("docs-mcp") is None
        assert await connectors_svc._credential_for("docs-mcp") is None
    assert NoCredsSM.calls == 2
    warnings = [r for r in caplog.records if "credential store unavailable" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not per probe
    assert "NoCredentialsError" in warnings[0].getMessage()

    class UnreachableSM:
        def get_secret_value(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://secretsmanager.example")

    monkeypatch.setattr(connectors_svc, "_secrets", lambda: UnreachableSM())
    assert await connectors_svc._credential_for("docs-mcp") is None


async def test_credential_for_still_raises_real_client_errors(monkeypatch):
    from botocore.exceptions import ClientError

    from app.services import connectors as connectors_svc

    class DeniedSM:
        def get_secret_value(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "GetSecretValue",
            )

    monkeypatch.setattr(connectors_svc, "_secrets", lambda: DeniedSM())
    with pytest.raises(ClientError):  # permission problems stay loud (not "no credential")
        await connectors_svc._credential_for("docs-mcp")


async def test_connector_copy_fails_closed_when_credential_store_unreachable(
    db_session, test_user, monkeypatch
):
    """The deploy-path read (credential_for) is STRICT: a credential-store
    failure must fail the deployment, never ship a {"value": null} copy into
    the target account. Only probe() degrades (the B7 fork-safe path)."""
    from botocore.exceptions import EndpointConnectionError

    from app.models import CodegenBuild, Deployment, Project
    from app.services import connectors as connectors_svc
    from app.services import deployment as deploy_svc

    await _registry_with(db_session, ("support-api", True))
    project = Project(user_id=test_user.id, name="C1-strict", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    build = CodegenBuild(
        project_id=project.id,
        status="ready",
        created_by=test_user.id,
        manifest={"declared_connectors": ["support-api"]},
    )
    db_session.add(build)
    await db_session.flush()
    deployment = Deployment(
        project_id=project.id, user_id=test_user.id, status="deploying", build_id=build.id
    )
    db_session.add(deployment)
    await db_session.commit()

    class UnreachableRegistrySM:
        def get_secret_value(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://secretsmanager.example")

    monkeypatch.setattr(connectors_svc, "_secrets", lambda: UnreachableRegistrySM())
    monkeypatch.setattr(connectors_svc, "_credential_backend_warned", False)

    written: list[dict] = []

    class TargetSM:
        def create_secret(self, **kwargs):
            written.append(kwargs)

        def put_secret_value(self, **kwargs):  # pragma: no cover — must not be reached
            written.append(kwargs)

    class TargetSession:
        def client(self, name):
            assert name == "secretsmanager"
            return TargetSM()

    with pytest.raises(EndpointConnectionError):  # propagates → _run_deploy marks failed
        await deploy_svc._ensure_connector_secrets(db_session, deployment, TargetSession())
    assert written == []  # no connector copy reaches the target account
    assert connectors_svc._credential_backend_warned is False  # strict path never degrades

    # The same failure on the probe path still degrades to "no credential".
    assert await connectors_svc._credential_for("support-api") is None
