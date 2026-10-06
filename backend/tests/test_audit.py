"""Audit trail + model-usage logging tests (audit-logging spec)."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import AuditLog, ModelInvocation
from app.services import audit
from app.services.bedrock import InvocationCtx, _record_invocation
from app.services.pricing import compute_cost_usd

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------- registry completeness


def _all_mutating_routes():
    from app.api.admin_audit import router as admin_audit
    from app.api.admin_governance import router as admin_governance
    from app.api.admin_integrations import router as admin_integrations
    from app.api.admin_service_accounts import router as admin_service_accounts
    from app.api.admin_users import router as admin_users
    from app.api.builds import router as builds
    from app.api.chat import router as chat
    from app.api.collab import router as collab
    from app.api.deployments import router as deployments
    from app.api.marketplace import admin_router as marketplace_admin
    from app.api.marketplace import admin_submissions_router as marketplace_admin_subs
    from app.api.marketplace import router as marketplace
    from app.api.marketplace import submissions_router as marketplace_subs
    from app.api.notifications import router as notifications
    from app.api.projects import router as projects
    from app.api.reviews import router as reviews
    from app.api.specs import router as specs
    from app.api.teams import admin_router as teams_admin
    from app.api.teams import router as teams
    from app.api.templates import admin_router as templates_admin
    from app.api.templates import router as templates
    from app.api.users import router as users

    routers = [
        builds, chat, collab, deployments, marketplace, marketplace_admin,
        marketplace_subs, marketplace_admin_subs, projects,
        specs, templates, templates_admin, users, admin_users, admin_audit,
        admin_governance, admin_integrations, admin_service_accounts,
        notifications, reviews, teams, teams_admin,
    ]
    for router in routers:
        for route in router.routes:
            for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
                yield method, route.path


async def test_registry_covers_mutating_routes():
    """Every mutating route must be audited or explicitly exempted (R1.4)."""
    missing = [
        (method, path)
        for method, path in _all_mutating_routes()
        if (method, path) not in audit.AUDIT_ROUTE_REGISTRY
        and (method, path) not in audit.AUDIT_EXEMPT
    ]
    assert missing == [], f"Unaudited mutating routes: {missing}"


async def test_registry_keys_are_real_routes():
    """No stale registry entries pointing at renamed/removed routes."""
    actual = set(_all_mutating_routes())
    stale = [k for k in audit.AUDIT_ROUTE_REGISTRY if k not in actual]
    assert stale == [], f"Registry entries without live routes: {stale}"


# ------------------------------------------------------------ middleware seam


async def test_project_create_writes_audit_row(client, db_session, audit_db, test_user):
    resp = await client.post("/api/v1/projects", json={"name": "Audit Me"})
    assert resp.status_code == 201
    await asyncio.sleep(0)  # let any scheduled task settle
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    actions = [r.action for r in rows]
    assert "project_created" in actions
    row = next(r for r in rows if r.action == "project_created")
    assert row.category == "user"
    assert row.actor_id == test_user.id
    assert row.http_status == 201


async def test_endpoint_detail_enrichment(client, db_session, audit_db):
    created = (await client.post("/api/v1/projects", json={"name": "Before"})).json()
    resp = await client.put(f"/api/v1/projects/{created['id']}", json={"name": "After"})
    assert resp.status_code == 200
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "project_updated"))).scalars().all()
    assert rows and rows[0].detail["before"]["name"] == "Before"
    assert rows[0].detail["after"]["name"] == "After"
    assert str(rows[0].project_id) == created["id"]


async def test_get_requests_not_audited(client, db_session, audit_db):
    await client.get("/api/v1/projects")
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert rows == []


async def test_audit_failure_does_not_fail_request(client, db_session, audit_db, monkeypatch):
    """R1.3: availability over completeness."""

    class Boom:
        def __call__(self):
            raise RuntimeError("audit db down")

    monkeypatch.setattr(audit, "SessionLocal", Boom())
    resp = await client.post("/api/v1/projects", json={"name": "Still Works"})
    assert resp.status_code == 201


async def test_security_event_on_unauthenticated(db_engine, audit_db):
    """401s land as security/auth_failed even with no user (R1.1)."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.db import get_db
    from app.main import create_app

    app = create_app()  # NO auth override — real 401 path
    maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/v1/projects", json={"name": "x"})
    assert resp.status_code == 401
    async with maker() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert any(r.category == "security" and r.action == "auth_failed" for r in rows)


# --------------------------------------------------------------- bedrock seam


async def test_record_invocation_prices_and_hashes(db_session, audit_db):
    ctx = InvocationCtx(purpose="chat", user_id=uuid.uuid4())
    await _record_invocation(
        ctx,
        model_id="us.anthropic.claude-sonnet-5",
        prompt="[system]\nhi\n\n[user]\nhello",
        response_text="world",
        input_tokens=1000,
        output_tokens=2000,
        stop_reason="end_turn",
        latency_ms=42,
        success=True,
    )
    row = (await db_session.execute(select(ModelInvocation))).scalar_one()
    assert row.purpose == "chat"
    assert row.cost_usd == Decimal("0.033")  # 1k*0.003 + 2k*0.015
    assert len(row.prompt_sha256) == 64 and len(row.response_sha256) == 64
    assert row.success is True


async def test_record_invocation_unknown_model_null_cost(db_session, audit_db):
    await _record_invocation(
        InvocationCtx(purpose="classification"),
        model_id="some.future-model",
        prompt="p", response_text="r",
        input_tokens=10, output_tokens=10,
        stop_reason=None, latency_ms=1, success=True,
    )
    row = (await db_session.execute(select(ModelInvocation))).scalar_one()
    assert row.cost_usd is None


async def test_record_invocation_failure_row(db_session, audit_db):
    await _record_invocation(
        InvocationCtx(purpose="design"),
        model_id="us.anthropic.claude-sonnet-5",
        prompt="p", response_text="",
        input_tokens=0, output_tokens=0,
        stop_reason=None, latency_ms=7,
        success=False, error_class="ThrottlingException",
    )
    row = (await db_session.execute(select(ModelInvocation))).scalar_one()
    assert row.success is False and row.error_class == "ThrottlingException"
    assert row.cost_usd is None


async def test_pricing_math():
    assert compute_cost_usd("us.anthropic.claude-sonnet-5", 1000, 1000) == Decimal("0.018")
    assert compute_cost_usd("unknown", 1, 1) is None


# --------------------------------------------- reasoning suppression (13.5Y)
# Adaptive-thinking Claude models emit reasoningContent blocks by default;
# the seam extracts text blocks only, so reasoning is billed-but-dropped and
# fixed-budget calls (codegen plan/assembly, risk, title) starve. The seam
# must pin thinking off for the Claude family and tolerate rejection.


class _FakeBedrockClient:
    def __init__(self, script):
        self.calls: list[dict] = []
        self._script = list(script)

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_converse_response(text="ok"):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "stopReason": "end_turn",
    }


def _stream_response(*events):
    """A converse_stream response whose event stream yields `events`."""
    return {"stream": iter(events)}


def _ok_stream_events(text="hello"):
    return (
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1},
                "metrics": {"latencyMs": 5},
            }
        },
    )


def _read_timeout():
    from botocore.exceptions import ReadTimeoutError

    return ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")


def _validation_error(message):
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": message}}, "Converse"
    )


def test_additional_request_fields_mapping():
    from app.services.bedrock import additional_request_fields

    assert additional_request_fields("us.anthropic.claude-sonnet-5") == {
        "thinking": {"type": "disabled"}
    }
    assert additional_request_fields(
        "anthropic.claude-haiku-4-5-20251001-v1:0"
    ) == {"thinking": {"type": "disabled"}}
    assert additional_request_fields("amazon.nova-pro-v1:0") == {}
    assert additional_request_fields("meta.llama3-3-70b-instruct-v1:0") == {}


async def test_converse_pins_thinking_off_for_claude(db_session, monkeypatch):
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient([_ok_converse_response()])
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    text, _usage, stop = await bedrock_mod.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    )
    assert text == "ok" and stop == "end_turn"
    assert fake.calls[0]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }


async def test_converse_no_thinking_field_for_other_families(db_session, monkeypatch):
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient([_ok_converse_response()])
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    await bedrock_mod.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="amazon.nova-pro-v1:0",
        max_tokens=100,
    )
    assert "additionalModelRequestFields" not in fake.calls[0]


async def test_converse_thinking_rejection_retries_without_field(db_session, monkeypatch):
    """Families that predate adaptive thinking refuse the field → drop and retry."""
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [
            _validation_error("This model does not support thinking configuration"),
            _ok_converse_response("fallback"),
        ]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    text, _usage, _stop = await bedrock_mod.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    )
    assert text == "fallback"
    assert fake.calls[0]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }
    assert "additionalModelRequestFields" not in fake.calls[1]
    # The optional-inference-config fallback stays intact after ours ran
    assert fake.calls[1]["inferenceConfig"] == fake.calls[0]["inferenceConfig"]


async def test_converse_retries_transient_invalid_model(db_session, monkeypatch):
    """Observed live (13.5Y): the inference profile intermittently rejects a
    valid model id mid-build. One retry must absorb it; a genuinely bad id
    still fails after MAX_ATTEMPTS."""
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [
            _validation_error("The provided model identifier is invalid."),
            _ok_converse_response("recovered"),
        ]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    text, _usage, _stop = await bedrock_mod.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    )
    assert text == "recovered"
    assert len(fake.calls) == 2
    # thinking suppression survives the retry (unrelated fallback untouched)
    assert fake.calls[1]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }


async def test_converse_retries_internal_server_exception(db_session, monkeypatch):
    """13.5Z: Bedrock's InternalServerException is a documented transient —
    it must ride the standard bounded retry, not crash a multi-call build."""
    from botocore.exceptions import ClientError

    from app.services import bedrock as bedrock_mod

    blip = ClientError(
        {
            "Error": {
                "Code": "InternalServerException",
                "Message": "The system encountered an unexpected error during processing.",
            }
        },
        "Converse",
    )
    fake = _FakeBedrockClient([blip, _ok_converse_response("after-blip")])
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    text, _usage, _stop = await bedrock_mod.converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    )
    assert text == "after-blip"
    assert len(fake.calls) == 2


async def test_converse_genuinely_invalid_model_still_fails(db_session, monkeypatch):
    from botocore.exceptions import ClientError

    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [_validation_error("The provided model identifier is invalid.")] * 3
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    with pytest.raises(ClientError):
        await bedrock_mod.converse(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="s",
            model_id="us.anthropic.claude-sonnet-5",
            max_tokens=100,
        )
    assert len(fake.calls) == 3  # bounded: MAX_ATTEMPTS, then the real error


async def test_stream_converse_pins_thinking_off(db_session, monkeypatch):
    from app.services import bedrock as bedrock_mod

    stream_events = {
        "stream": iter(
            [
                {"contentBlockDelta": {"delta": {"text": "hello"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {
                    "metadata": {
                        "usage": {"inputTokens": 1, "outputTokens": 1},
                        "metrics": {"latencyMs": 5},
                    }
                },
            ]
        )
    }
    fake = _FakeBedrockClient([stream_events])
    monkeypatch.setattr(bedrock_mod, "bedrock_stream_client", lambda: fake)
    parts = []
    async for delta in bedrock_mod.stream_converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    ):
        parts.append(delta)
    assert "".join(parts) == "hello"
    assert fake.calls[0]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }


# First-token timeout (OSS code wave): the stream uses a dedicated client with
# a short read timeout so a stalled model is retried within ~45 s rather than
# the 180 s the non-streaming converse path legitimately needs.


async def test_stream_converse_retries_read_timeout_before_first_token(db_session, monkeypatch):
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient([_read_timeout(), _stream_response(*_ok_stream_events("hello"))])
    monkeypatch.setattr(bedrock_mod, "bedrock_stream_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    parts = []
    async for delta in bedrock_mod.stream_converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
    ):
        parts.append(delta)
    assert "".join(parts) == "hello"
    assert len(fake.calls) == 2  # one timeout before the first token → one retry
    # thinking suppression survives the retry
    assert fake.calls[1]["additionalModelRequestFields"] == {
        "thinking": {"type": "disabled"}
    }


async def test_stream_converse_read_timeout_after_first_token_is_not_retried(
    db_session, monkeypatch, caplog
):
    from botocore.exceptions import ReadTimeoutError

    from app.services import bedrock as bedrock_mod

    def stalled_stream():
        yield {"contentBlockDelta": {"delta": {"text": "hello"}}}
        raise _read_timeout()

    # The second scripted response must never be consumed: no retry after a token.
    fake = _FakeBedrockClient(
        [{"stream": stalled_stream()}, _stream_response(*_ok_stream_events("never"))]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_stream_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    result = bedrock_mod.StreamResult()
    parts = []
    with caplog.at_level("ERROR", logger="marshal.bedrock"):
        with pytest.raises(ReadTimeoutError):
            async for delta in bedrock_mod.stream_converse(
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                system="s",
                model_id="us.anthropic.claude-sonnet-5",
                max_tokens=100,
                result=result,
            ):
                parts.append(delta)
    assert parts == ["hello"]
    assert len(fake.calls) == 1
    assert result.text == "hello"  # partial text retained for the failure record
    assert "stopped responding mid-stream after 5 chars" in caplog.text


def test_stream_client_timeouts(monkeypatch):
    """The stream client fails fast; the non-streaming client keeps 180 s."""
    from app.services import bedrock as bedrock_mod

    # Other tests may have left fakes in the module-level caches.
    monkeypatch.setattr(bedrock_mod, "_client", None)
    monkeypatch.setattr(bedrock_mod, "_stream_client", None)
    assert bedrock_mod.FIRST_TOKEN_TIMEOUT_S == 45
    stream_config = bedrock_mod.bedrock_stream_client().meta.config
    assert stream_config.read_timeout == bedrock_mod.FIRST_TOKEN_TIMEOUT_S
    assert stream_config.connect_timeout == 10
    # botocore normalizes retries={"max_attempts": 0} to one total attempt:
    # retry policy stays in stream_converse, not in the SDK.
    assert stream_config.retries["total_max_attempts"] == 1
    assert bedrock_mod.bedrock_client().meta.config.read_timeout == 180
    assert bedrock_mod.bedrock_client() is not bedrock_mod.bedrock_stream_client()


# ------------------- deprecated optional inference parameters (hotfix 5 Oct)
# Claude Sonnet 5 rejects `temperature` with "`temperature` is deprecated for
# this model" (ValidationException). The helper only knew the not-supported /
# unsupported wordings, so risk scoring (temperature 0 on the fast model)
# failed outright on every sonnet-5-only template and Deploy returned 403
# risk_error. "deprecated" is now a rejection marker: drop the optional field,
# retry once with maxTokens only.


@pytest.mark.parametrize(
    "message, config",
    [
        (
            "`temperature` is deprecated for this model",
            {"maxTokens": 600, "temperature": 0.0},
        ),
        (
            "Malformed input request: `Temperature` is DEPRECATED for this model.",
            {"maxTokens": 600, "temperature": 0.0},
        ),
        ("`top_p` is deprecated for this model", {"maxTokens": 600, "topP": 0.9}),
        ("`top_k` is deprecated for this model", {"maxTokens": 600, "temperature": 0.0}),
        # The pre-existing wordings keep working alongside the new marker.
        ("temperature is not supported for this model", {"maxTokens": 600, "temperature": 0.0}),
    ],
)
def test_deprecated_wording_is_a_rejected_optional_parameter(message, config):
    from app.services.bedrock import _optional_inference_config_rejected

    assert _optional_inference_config_rejected(_validation_error(message), config) is True


@pytest.mark.parametrize(
    "message, config",
    [
        # Nothing optional left to drop: a maxTokens-only request is never retried.
        ("`temperature` is deprecated for this model", {"maxTokens": 600}),
        # A deprecated MODEL names no optional field — not our fallback.
        (
            "The model us.anthropic.claude-example is deprecated; migrate to a newer model.",
            {"maxTokens": 600, "temperature": 0.0},
        ),
        # Unrelated validation failure that happens to mention the field.
        (
            "Malformed input request: #/messages/0/content: expected minimum item count: 1",
            {"maxTokens": 600, "temperature": 0.0},
        ),
    ],
)
def test_deprecated_wording_is_not_over_matched(message, config):
    from botocore.exceptions import ClientError

    from app.services.bedrock import _optional_inference_config_rejected

    assert _optional_inference_config_rejected(_validation_error(message), config) is False
    # A non-validation code with the same words is a different failure class.
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": message}}, "Converse"
    )
    assert _optional_inference_config_rejected(throttled, config) is False


async def test_converse_deprecated_temperature_retries_without_it(
    db_session, monkeypatch, caplog
):
    """The risk scorer's exact shape: converse(..., temperature=0.0) on sonnet-5.
    First call carries temperature and is refused as deprecated; the retry
    sends maxTokens only and succeeds. The WARNING names the model and the
    dropped field, and never carries prompt content."""
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [
            _validation_error("`temperature` is deprecated for this model"),
            _ok_converse_response('{"synthetic": "rubric"}'),
        ]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    prompt = "SYNTHETIC-PROMPT-7f3a: not to appear in logs"
    with caplog.at_level("WARNING", logger="marshal.bedrock"):
        text, _usage, stop = await bedrock_mod.converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system="synthetic system prompt",
            model_id="us.anthropic.claude-sonnet-5",
            max_tokens=600,
            temperature=0.0,
        )
    assert text == '{"synthetic": "rubric"}' and stop == "end_turn"
    assert len(fake.calls) == 2
    assert fake.calls[0]["inferenceConfig"] == {"maxTokens": 600, "temperature": 0.0}
    assert fake.calls[1]["inferenceConfig"] == {"maxTokens": 600}
    # Only the optional field is dropped — thinking suppression and the
    # messages/system payload ride the retry unchanged.
    assert fake.calls[1]["additionalModelRequestFields"] == {"thinking": {"type": "disabled"}}
    assert fake.calls[1]["messages"] == fake.calls[0]["messages"]
    warnings = [
        r for r in caplog.records
        if r.name == "marshal.bedrock" and r.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert "us.anthropic.claude-sonnet-5" in warnings[0].getMessage()
    assert "temperature" in warnings[0].getMessage()
    assert "SYNTHETIC-PROMPT-7f3a" not in caplog.text
    assert "synthetic system prompt" not in caplog.text


async def test_stream_converse_deprecated_temperature_retries_without_it(
    db_session, monkeypatch
):
    """Same helper on the streaming path (chat sends the session temperature)."""
    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [
            _validation_error("`temperature` is deprecated for this model"),
            _stream_response(*_ok_stream_events("hello")),
        ]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_stream_client", lambda: fake)
    parts = []
    async for delta in bedrock_mod.stream_converse(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        system="s",
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=100,
        temperature=0.7,
    ):
        parts.append(delta)
    assert "".join(parts) == "hello"
    assert len(fake.calls) == 2
    assert fake.calls[0]["inferenceConfig"] == {"maxTokens": 100, "temperature": 0.7}
    assert fake.calls[1]["inferenceConfig"] == {"maxTokens": 100}


async def test_converse_unrelated_validation_error_is_not_retried(db_session, monkeypatch):
    """Regression: a ValidationException that is not an optional-parameter
    rejection is raised on the first attempt, even with temperature present."""
    from botocore.exceptions import ClientError

    from app.services import bedrock as bedrock_mod

    fake = _FakeBedrockClient(
        [
            _validation_error(
                "Malformed input request: #/messages/0/content: expected minimum item count: 1"
            ),
            _ok_converse_response("must not be reached"),
        ]
    )
    monkeypatch.setattr(bedrock_mod, "bedrock_client", lambda: fake)
    monkeypatch.setattr(bedrock_mod, "_backoff", lambda _a: 0.0)
    with pytest.raises(ClientError) as excinfo:
        await bedrock_mod.converse(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="s",
            model_id="us.anthropic.claude-sonnet-5",
            max_tokens=600,
            temperature=0.0,
        )
    assert excinfo.value.response["Error"]["Code"] == "ValidationException"
    assert len(fake.calls) == 1
    assert fake.calls[0]["inferenceConfig"] == {"maxTokens": 600, "temperature": 0.0}


# ------------------------------------------------------------- admin audit API


async def test_admin_audit_list_and_detail(admin_user, client_for, db_session, audit_db):
    async with client_for(admin_user) as ac:
        await ac.post("/api/v1/projects", json={"name": "P1"})
        resp = await ac.get("/api/v1/admin/audit-logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        entry = next(i for i in body["items"] if i["action"] == "project_created")
        detail = await ac.get(f"/api/v1/admin/audit-logs/{entry['id']}")
        assert detail.status_code == 200
        assert detail.json()["source"] == "audit"


async def test_admin_audit_unified_model_timeline(admin_user, client_for, db_session, audit_db):
    await _record_invocation(
        InvocationCtx(purpose="chat", user_id=admin_user.id),
        model_id="us.anthropic.claude-sonnet-5",
        prompt="p", response_text="r",
        input_tokens=5, output_tokens=5,
        stop_reason="end_turn", latency_ms=3, success=True,
    )
    async with client_for(admin_user) as ac:
        resp = await ac.get("/api/v1/admin/audit-logs", params={"category": "model"})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["action"] == "bedrock_chat"
        assert body["items"][0]["source"] == "model"
        detail = await ac.get(f"/api/v1/admin/audit-logs/{body['items'][0]['id']}")
        assert detail.json()["detail"]["prompt_text"] == "p"


async def test_admin_audit_export_csv(admin_user, client_for, audit_db):
    async with client_for(admin_user) as ac:
        await ac.post("/api/v1/projects", json={"name": "CSV Row"})
        resp = await ac.post("/api/v1/admin/audit-logs/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        text = resp.text
        assert "project_created" in text and "created_at" in text


async def test_admin_audit_role_gate(client, audit_db):
    """Power user (default client fixture) must get 403 (R3.5)."""
    resp = await client.get("/api/v1/admin/audit-logs")
    assert resp.status_code == 403
