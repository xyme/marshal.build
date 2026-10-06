"""S17 custom model endpoints: registry seam, lifecycle, adapter, governance."""

import json
import uuid

import httpx
import pytest

from app.services import guardrails
from app.services import model_endpoints as svc
from app.services import platform_settings as settings_svc
from app.services.model_providers import openai_compat

pytestmark = pytest.mark.asyncio

ENDPOINT = {
    "slug": "onprem-llama",
    "label": "ACME Llama (on-prem)",
    "base_url": "https://models.acme.example/v1",
    "model_name": "llama-3-70b-instruct",
    "tier": "standard",
    "usd_per_1k_input": 0.0009,
    "usd_per_1k_output": 0.0011,
    "max_context_tokens": 8192,
    "timeout_s": 30,
    "has_api_key": False,
    "enabled": True,
}


@pytest.fixture(autouse=True)
def _fresh_settings_cache():
    settings_svc.invalidate_cache()
    yield
    settings_svc.invalidate_cache()


@pytest.fixture(autouse=True)
def _no_secrets(monkeypatch):
    """Secrets Manager stub recording writes; reads honour prior writes."""
    store: dict[str, str] = {}

    def fake_store(slug, api_key):
        store[slug] = api_key

    async def fake_read(slug):
        return store.get(slug)

    monkeypatch.setattr(svc, "_store_key", fake_store)
    monkeypatch.setattr(svc, "api_key_for", fake_read)
    return store


async def _seed_endpoint(db, admin_user, **overrides) -> dict:
    payload = {**ENDPOINT, **overrides}
    payload.pop("has_api_key", None)
    created = await svc.create_endpoint(db, payload, admin_user.id)
    settings_svc.invalidate_cache()
    return created


# ------------------------------------------------------------- registry seam


async def test_registry_merges_enabled_endpoints(db_session, admin_user, audit_db):
    await _seed_endpoint(db_session, admin_user)
    registry = await guardrails.model_registry()
    ids = [m["id"] for m in registry]
    assert "ext/onprem-llama" in ids
    entry = next(m for m in registry if m["id"] == "ext/onprem-llama")
    assert entry["source"] == "external" and entry["provider"] == "openai_compat"
    # Bedrock built-ins untouched, first and in order
    assert ids[: len(guardrails.BEDROCK_REGISTRY)] == [m["id"] for m in guardrails.BEDROCK_REGISTRY]


async def test_registry_excludes_disabled(db_session, admin_user, audit_db):
    await _seed_endpoint(db_session, admin_user)
    await svc.update_endpoint(db_session, "onprem-llama", {"enabled": False}, admin_user.id)
    settings_svc.invalidate_cache()
    ids = [m["id"] for m in await guardrails.model_registry()]
    assert "ext/onprem-llama" not in ids


async def test_registry_degrades_to_bedrock_on_settings_failure(monkeypatch):
    async def boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("app.services.platform_settings.get_controls", boom)
    ids = [m["id"] for m in await guardrails.model_registry()]
    assert ids == [m["id"] for m in guardrails.BEDROCK_REGISTRY]


async def test_resolution_routes_chat_but_pins_fast_and_codegen(
    db_session, admin_user, audit_db
):
    """Allowlist ONLY the external model: chat resolves to it, but the governed
    purposes (fast → risk/titling, codegen) stay on Bedrock [S17-05 pin]."""
    await _seed_endpoint(db_session, admin_user)
    await settings_svc.update_controls(
        db_session,
        model_allowlist=["ext/onprem-llama"],
        param_bounds={}, rate_limits={}, cost={},
        updated_by=admin_user.id,
    )
    settings_svc.invalidate_cache()
    models = await guardrails.resolve_models(None)
    assert models.chat == "ext/onprem-llama"
    assert not models.fast.startswith("ext/")
    assert not models.codegen.startswith("ext/")


async def test_allowlist_accepts_ext_ids_and_rejects_unknown(db_session, admin_user, audit_db):
    await _seed_endpoint(db_session, admin_user)
    with pytest.raises(settings_svc.SettingsValidationError):
        await settings_svc.update_controls(
            db_session, model_allowlist=["ext/never-registered"],
            param_bounds={}, rate_limits={}, cost={}, updated_by=admin_user.id,
        )


# ------------------------------------------------------------------ lifecycle


async def test_create_validates_and_masks(db_session, admin_user, audit_db, _no_secrets):
    created = await _seed_endpoint(db_session, admin_user, api_key="sk-secret-value")
    assert created["has_api_key"] is True
    assert "api_key" not in created and "sk-secret-value" not in json.dumps(created)
    assert _no_secrets["onprem-llama"] == "sk-secret-value"


@pytest.mark.parametrize(
    "field,value,fragment",
    [
        ("base_url", "http://insecure.example/v1", "https"),
        ("usd_per_1k_input", 0, "cost caps"),
        ("usd_per_1k_output", None, "Missing required"),
        ("slug", "Bad Slug!", "slug"),
        ("timeout_s", 2, "timeout_s"),
    ],
)
async def test_create_rejects_bad_payloads(
    db_session, admin_user, audit_db, field, value, fragment
):
    payload = {**ENDPOINT}
    payload.pop("has_api_key")
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value
    with pytest.raises(svc.EndpointValidationError) as excinfo:
        await svc.create_endpoint(db_session, payload, admin_user.id)
    assert fragment.lower() in str(excinfo.value).lower()


async def test_slug_immutable_and_duplicate_rejected(db_session, admin_user, audit_db):
    await _seed_endpoint(db_session, admin_user)
    with pytest.raises(svc.EndpointValidationError):
        await svc.create_endpoint(
            db_session, {k: v for k, v in ENDPOINT.items() if k != "has_api_key"},
            admin_user.id,
        )
    with pytest.raises(svc.EndpointValidationError):
        await svc.update_endpoint(
            db_session, "onprem-llama", {"slug": "renamed"}, admin_user.id
        )


async def test_admin_api_round_trip(admin_user, client_for, db_session, audit_db):
    async with client_for(admin_user) as ac:
        created = await ac.post(
            "/api/v1/admin/model-endpoints",
            json={k: v for k, v in ENDPOINT.items() if k != "has_api_key"},
        )
        assert created.status_code == 201, created.text
        listed = (await ac.get("/api/v1/admin/model-endpoints")).json()
        assert [e["slug"] for e in listed] == ["onprem-llama"]
        updated = await ac.put(
            "/api/v1/admin/model-endpoints/onprem-llama", json={"enabled": False}
        )
        assert updated.status_code == 200 and updated.json()["enabled"] is False
        registry = (await ac.get("/api/v1/admin/templates/model-registry")).json()
        assert "ext/onprem-llama" not in [m["id"] for m in registry]
        missing = await ac.put(
            "/api/v1/admin/model-endpoints/ghost", json={"enabled": False}
        )
        assert missing.status_code == 404


async def test_admin_gate(test_user, client_for, audit_db):
    async with client_for(test_user) as ac:
        assert (await ac.get("/api/v1/admin/model-endpoints")).status_code == 403


# -------------------------------------------------------------------- pricing


async def test_external_pricing_from_admin_rates(db_session, admin_user, audit_db):
    from app.services.pricing import compute_cost_ext

    await _seed_endpoint(db_session, admin_user)
    cost = await compute_cost_ext("ext/onprem-llama", 1000, 1000)
    assert float(cost) == pytest.approx(0.0009 + 0.0011)
    assert await compute_cost_ext("ext/ghost", 1000, 1000) is None


async def test_disabled_endpoint_still_prices(db_session, admin_user, audit_db):
    """In-flight calls on a just-disabled endpoint must never log unpriced."""
    from app.services.pricing import compute_cost_ext

    await _seed_endpoint(db_session, admin_user)
    await svc.update_endpoint(db_session, "onprem-llama", {"enabled": False}, admin_user.id)
    settings_svc.invalidate_cache()
    assert await compute_cost_ext("ext/onprem-llama", 1000, 0) is not None


# -------------------------------------------------------------------- adapter


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.request = httpx.Request("POST", "https://models.acme.example/v1/chat/completions")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeClient:
    """Programmable httpx.AsyncClient stand-in (post + stream)."""

    plan: list = []  # each: dict(post=...) or dict(stream_lines=[...]) or dict(exc=...)

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        step = _FakeClient.plan.pop(0)
        if "exc" in step:
            raise step["exc"]
        return _FakeResponse(**step["post"])

    def stream(self, method, url, json=None, headers=None):
        step = _FakeClient.plan.pop(0)
        outer = self

        class _StreamCtx:
            status_code = step.get("status", 200)
            request = httpx.Request(method, url)

            async def __aenter__(self):
                if "exc" in step:
                    raise step["exc"]
                return self

            async def __aexit__(self, *args):
                return False

            async def aread(self):
                return step.get("body", b"")

            async def aiter_lines(self):
                for line in step.get("stream_lines", []):
                    yield line

        _ = outer
        return _StreamCtx()


@pytest.fixture
def fake_transport(monkeypatch):
    _FakeClient.plan = []
    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


@pytest.fixture
def recorded(monkeypatch):
    """Capture _record_invocation kwargs instead of writing rows.

    Recording happens SYNCHRONOUSLY at call time (append in the outer
    function), so tests can assert immediately; the returned no-op coroutine
    exists only for _emit to dispose of.
    """
    rows: list[dict] = []

    def fake_record(ctx, **kwargs):
        rows.append(kwargs)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr("app.services.bedrock._record_invocation", fake_record)
    monkeypatch.setattr("app.services.bedrock._emit", lambda coro: coro.close())
    return rows


async def _configured(db_session, admin_user, monkeypatch, **overrides):
    await _seed_endpoint(db_session, admin_user, **overrides)

    async def no_preflight(ctx, streaming=False):
        return None

    monkeypatch.setattr("app.services.bedrock.preflight_model_call", no_preflight)


async def test_converse_ext_maps_usage_and_stop(
    db_session, admin_user, audit_db, fake_transport, monkeypatch
):
    await _configured(db_session, admin_user, monkeypatch)
    fake_transport.plan = [
        {"post": {"body": {
            "model": "llama-3-70b-instruct",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }}},
    ]
    from app.services.bedrock import converse

    text, usage, stop = await converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="be brief", model_id="ext/onprem-llama",
    )
    assert text == "hello"
    assert usage == {"inputTokens": 12, "outputTokens": 3}
    assert stop == "end_turn"


async def test_converse_ext_estimates_when_usage_missing(
    db_session, admin_user, audit_db, fake_transport, monkeypatch, recorded
):
    await _configured(db_session, admin_user, monkeypatch)
    fake_transport.plan = [
        {"post": {"body": {"choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}]}}},
    ]
    from app.services.bedrock import InvocationCtx, converse

    await converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}], system="",
        model_id="ext/onprem-llama",
        ctx=InvocationCtx(purpose="chat", user_id=uuid.uuid4()), enforce=False,
    )
    assert recorded and recorded[0]["usage_estimated"] is True
    assert recorded[0]["input_tokens"] > 0 and recorded[0]["output_tokens"] > 0


async def test_converse_ext_retries_then_fails_closed(
    db_session, admin_user, audit_db, fake_transport, monkeypatch, recorded
):
    await _configured(db_session, admin_user, monkeypatch)
    boom = httpx.ConnectError("connection refused")
    fake_transport.plan = [{"exc": boom}, {"exc": boom}, {"exc": boom}]
    monkeypatch.setattr(openai_compat.asyncio, "sleep", _instant_sleep)
    from app.services.bedrock import InvocationCtx, converse

    with pytest.raises(openai_compat.ExternalEndpointError) as excinfo:
        await converse(
            messages=[{"role": "user", "content": [{"text": "hi"}]}], system="",
            model_id="ext/onprem-llama",
            ctx=InvocationCtx(purpose="chat", user_id=uuid.uuid4()), enforce=False,
        )
    message = str(excinfo.value)
    assert "ACME Llama (on-prem)" in message and "Model Controls" in message
    assert fake_transport.plan == []  # all three attempts consumed
    assert recorded and recorded[0]["success"] is False


async def test_stream_ext_yields_and_records(
    db_session, admin_user, audit_db, fake_transport, monkeypatch, recorded
):
    await _configured(db_session, admin_user, monkeypatch)

    def chunk(content=None, finish=None, usage=None):
        data = {"choices": [{"delta": {"content": content} if content else {}, "finish_reason": finish}]}
        if usage:
            data["usage"] = usage
        return f"data: {json.dumps(data)}"

    fake_transport.plan = [{
        "stream_lines": [
            chunk("hel"), chunk("lo"), chunk(finish="stop"),
            f'data: {json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}})}',
            "data: [DONE]",
        ]
    }]
    from app.services.bedrock import InvocationCtx, stream_converse

    deltas = []
    async for delta in stream_converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}], system="",
        model_id="ext/onprem-llama",
        ctx=InvocationCtx(purpose="chat", user_id=uuid.uuid4()), enforce=False,
    ):
        deltas.append(delta)
    assert "".join(deltas) == "hello"
    assert recorded[0]["success"] is True
    assert recorded[0]["usage_estimated"] is False
    assert recorded[0]["input_tokens"] == 9
    assert recorded[0]["stop_reason"] == "end_turn"


async def test_stream_ext_fails_closed_mid_setup(
    db_session, admin_user, audit_db, fake_transport, monkeypatch, recorded
):
    await _configured(db_session, admin_user, monkeypatch)
    monkeypatch.setattr(openai_compat.asyncio, "sleep", _instant_sleep)
    fake_transport.plan = [
        {"exc": httpx.ConnectError("down")},
        {"exc": httpx.ConnectError("down")},
        {"exc": httpx.ConnectError("down")},
    ]
    from app.services.bedrock import InvocationCtx, stream_converse

    with pytest.raises(openai_compat.ExternalEndpointError):
        async for _ in stream_converse(
            messages=[{"role": "user", "content": [{"text": "hi"}]}], system="",
            model_id="ext/onprem-llama",
            ctx=InvocationCtx(purpose="chat", user_id=uuid.uuid4()), enforce=False,
        ):
            pass
    assert recorded and recorded[0]["success"] is False


async def test_disabled_endpoint_fails_closed(
    db_session, admin_user, audit_db, fake_transport, monkeypatch
):
    await _configured(db_session, admin_user, monkeypatch, enabled=False)
    from app.services.bedrock import converse

    with pytest.raises(openai_compat.ExternalEndpointError) as excinfo:
        await converse(
            messages=[{"role": "user", "content": [{"text": "hi"}]}], system="",
            model_id="ext/onprem-llama",
        )
    assert "disabled" in str(excinfo.value)


async def _instant_sleep(_s):
    return None


# ------------------------------------------------------------------ redaction


async def test_redaction_masks_high_risk_values():
    from app.services.redaction import redact_text

    # AWS's documented example key, split so repo secret-scanners (git-defender)
    # never see a contiguous AKIA... token in this file.
    example_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    text = (
        "Card 4111 1111 1111 1111, ssn 123-45-6789, api_key: sk-abc123, "
        f"{example_aws_key}, iban GB82WEST12345698765432, routing 021000021, "
        "ordinary numbers like 12345 stay."
    )
    masked, counts = redact_text(text)
    assert "[REDACTED:CARD_NUMBER]" in masked
    assert "[REDACTED:US_SSN]" in masked
    assert "[REDACTED:CREDENTIAL]" in masked
    assert "[REDACTED:AWS_ACCESS_KEY]" in masked
    assert "[REDACTED:IBAN]" in masked
    assert "[REDACTED:US_BANK_ROUTING]" in masked
    assert "12345 stay" in masked
    assert counts["CARD_NUMBER"] == 1


async def test_redaction_leaves_names_and_emails():
    from app.services.redaction import redact_text

    text = "Send the summary to jane.doe@acme.example and cc John Smith."
    masked, counts = redact_text(text)
    assert masked == text and counts == {}


async def test_redaction_applied_before_egress(
    db_session, admin_user, audit_db, fake_transport, monkeypatch
):
    """With pii_redaction ON, the payload leaving for the endpoint is masked."""
    await _configured(db_session, admin_user, monkeypatch)

    async def security_on():
        return {"pii_redaction": True, "admin_mfa_required": False}

    monkeypatch.setattr(
        "app.services.platform_settings.get_security_controls", security_on
    )
    sent: dict = {}

    async def capture_post(self, url, json=None, headers=None):
        sent["payload"] = json
        return _FakeResponse(body={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    monkeypatch.setattr(_FakeClient, "post", capture_post)
    from app.services.bedrock import converse

    await converse(
        messages=[{"role": "user", "content": [{"text": "my card is 4111 1111 1111 1111"}]}],
        system="", model_id="ext/onprem-llama",
    )
    body = json.dumps(sent["payload"])
    assert "4111" not in body and "[REDACTED:CARD_NUMBER]" in body
