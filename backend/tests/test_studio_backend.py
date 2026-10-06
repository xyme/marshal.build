"""Studio build experience backend (S18-01): prompt-context + effective-params
+ feature flag semantics.

Spec: .kiro/specs/studio-build-experience — R1.2 (flag/dark-launch), R3.2
(clamp truthfulness: display shares the enforcement path), R3.3 (session
updates through the existing PATCH + audit).
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import AuditLog, ChatSession, PlatformSettings, Template
from app.services import chat as chat_service
from app.services import platform_settings as settings_svc
from app.services.guardrails import resolve_session_call

pytestmark = pytest.mark.asyncio

SONNET = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    settings_svc.invalidate_cache()
    yield
    settings_svc.invalidate_cache()


@pytest.fixture
def seeded_row(db_session):
    """PlatformSettings id=1 (migration seeds it; create_all path makes it here)."""

    async def seed(**overrides) -> PlatformSettings:
        fields: dict = {
            "id": 1,
            "model_allowlist": [SONNET, HAIKU],
            "param_bounds": {
                "temperature": {"min": 0.0, "max": 1.0},
                "top_p": {"min": 0.0, "max": 1.0},
                "max_tokens": 8192,
                "max_context": 200000,
            },
            "rate_limits": {},
            "cost": {},
        }
        fields.update(overrides)
        row = PlatformSettings(**fields)
        db_session.add(row)
        await db_session.commit()
        settings_svc.invalidate_cache()
        return row

    return seed


@pytest.fixture
def clamping_template(db_session, admin_user):
    """The R3.2 fixture: narrows models to HAIKU, temp to <=0.7, tokens to 2048."""

    async def make() -> Template:
        template = Template(
            name="FS Regulated",
            status="active",
            guardrails={
                "model": {
                    "allowed_models": [HAIKU],
                    "max_tokens": 2048,
                    "temperature": {"min": 0.0, "max": 0.7},
                }
            },
            created_by=admin_user.id,
        )
        db_session.add(template)
        await db_session.commit()
        await db_session.refresh(template)
        return template

    return make


async def _mk_session(db_session, user, template_id=None, params=None) -> ChatSession:
    session = ChatSession(user_id=user.id, template_id=template_id, params=params)
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def _flag_studio(db_session, enabled: bool) -> None:
    row = await db_session.get(PlatformSettings, 1)
    row.feature_flags = {**(row.feature_flags or {}), "studio_enabled": enabled}
    await db_session.commit()
    settings_svc.invalidate_cache()


# ------------------------------------------------------------ access matrix


async def test_prompt_context_role_matrix(
    db_session, seeded_row, test_user, admin_user, business_user, client_for
):
    await seeded_row()
    session = await _mk_session(db_session, test_user)

    # Flag OFF: non-admin creator gets 404 (dark launch), admin previews 200.
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
        assert r.status_code == 404
    admin_session = await _mk_session(db_session, admin_user)
    async with client_for(admin_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{admin_session.id}/prompt-context")
        assert r.status_code == 200

    # Flag ON: power creator 200; business persona 403 [D17]; outsider 404.
    await _flag_studio(db_session, True)
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
        assert r.status_code == 200
    biz_session = await _mk_session(db_session, business_user)
    async with client_for(business_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{biz_session.id}/prompt-context")
        assert r.status_code == 403
    async with client_for(business_user) as ac:  # not the creator, no project
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
        assert r.status_code in (403, 404)  # session access 404s before persona 403


async def test_prompt_context_segments(
    db_session, seeded_row, test_user, client_for, clamping_template
):
    await seeded_row(feature_flags={"studio_enabled": True})
    template = await clamping_template()
    session = await _mk_session(
        db_session, test_user, template_id=template.id, params={"temperature": 0.5}
    )
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
    assert r.status_code == 200
    body = r.json()
    labels = [s["label"] for s in body["segments"]]
    assert labels[0] == "Persona framing"
    assert any("Template guardrail block" in label for label in labels)
    joined = " ".join(s["content"] for s in body["segments"])
    assert "FS Regulated" in joined
    assert "max_tokens=2048" in joined and "temperature=0.5" in joined
    assert body["model_id"] == HAIKU
    assert body["params"]["max_tokens"] == 2048


# ------------------------------------------------------- clamp truthfulness


async def test_effective_params_template_clamps(
    db_session, seeded_row, test_user, client_for, clamping_template
):
    await seeded_row(feature_flags={"studio_enabled": True})
    template = await clamping_template()
    session = await _mk_session(
        db_session,
        test_user,
        template_id=template.id,
        params={"model_id": SONNET, "temperature": 0.9, "max_tokens": 99999},
    )
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/effective-params")
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == {"model_id": SONNET, "temperature": 0.9, "max_tokens": 99999}
    assert body["effective"]["model_id"] == HAIKU  # SONNET excluded by template
    assert body["effective"]["temperature"] == 0.7
    assert body["effective"]["max_tokens"] == 2048
    assert body["clamped_by"]["model_id"] == "template 'FS Regulated'"
    assert body["clamped_by"]["temperature"] == "template 'FS Regulated'"
    assert body["clamped_by"]["max_tokens"] == "template 'FS Regulated'"
    assert body["allowed_models"] == [HAIKU]
    assert body["template_name"] == "FS Regulated"


async def test_effective_params_platform_clamp_and_defaults(
    db_session, seeded_row, test_user, client_for
):
    await seeded_row(
        param_bounds={
            "temperature": {"min": 0.0, "max": 0.8},
            "top_p": {"min": 0.0, "max": 1.0},
            "max_tokens": 4096,
            "max_context": 200000,
        },
        feature_flags={"studio_enabled": True},
    )
    # No overrides: requested nulls, nothing clamped.
    plain = await _mk_session(db_session, test_user)
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{plain.id}/effective-params")
        assert r.status_code == 200
        body = r.json()
        assert body["requested"] == {"model_id": None, "temperature": None, "max_tokens": None}
        assert body["clamped_by"] == {"model_id": None, "temperature": None, "max_tokens": None}
        assert body["effective"]["max_tokens"] == 4096

        # Platform bounds clamp an untemplated session.
        hot = await _mk_session(
            db_session, test_user, params={"temperature": 0.95, "max_tokens": 9000}
        )
        r = await ac.get(f"/api/v1/chat/sessions/{hot.id}/effective-params")
        body = r.json()
        assert body["effective"]["temperature"] == 0.8
        assert body["clamped_by"]["temperature"] == "platform"
        assert body["effective"]["max_tokens"] == 4096
        assert body["clamped_by"]["max_tokens"] == "platform"


async def test_send_message_uses_the_same_plan(
    db_session, seeded_row, test_user, client_for, clamping_template, monkeypatch
):
    """Enforcement parity (R3.2): the invocation seam receives EXACTLY what
    effective-params displays — same resolve_session_call output."""
    await seeded_row(feature_flags={"studio_enabled": True})
    template = await clamping_template()
    session = await _mk_session(
        db_session,
        test_user,
        template_id=template.id,
        params={"model_id": SONNET, "temperature": 0.9, "max_tokens": 99999},
    )

    store: dict[str, list[dict]] = {}

    async def put_message(session_id, role, content, **kwargs):
        store.setdefault(str(session_id), []).append({"role": role, "content": content})

    async def get_messages(session_id):
        return list(store.get(str(session_id), []))

    captured: dict = {}

    async def fake_stream(**kwargs):
        captured.update(kwargs)
        yield "ok"

    async def no_preflight(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_service, "put_message", put_message)
    monkeypatch.setattr(chat_service, "get_messages", get_messages)
    monkeypatch.setattr("app.api.chat.stream_converse", fake_stream)
    monkeypatch.setattr("app.api.chat.preflight_model_call", no_preflight)

    async with client_for(test_user) as ac:
        r = await ac.post(
            f"/api/v1/chat/sessions/{session.id}/messages", json={"content": "hi"}
        )
        assert r.status_code == 200
        plan = await resolve_session_call(template, session.params)

    assert captured["model_id"] == plan.model_id == HAIKU
    assert captured["temperature"] == plan.temperature == 0.7
    assert captured["max_tokens"] == plan.max_tokens == 2048


# ------------------------------------------------------------- PATCH session


async def test_patch_session_params_and_audit(
    db_session, seeded_row, test_user, client_for, clamping_template, audit_db
):
    await seeded_row()
    template = await clamping_template()
    session = await _mk_session(db_session, test_user, template_id=template.id)
    async with client_for(test_user) as ac:
        # Requested values persist verbatim (clamping is call-time, R3.3).
        r = await ac.patch(
            f"/api/v1/chat/sessions/{session.id}",
            json={"model_id": HAIKU, "temperature": 0.4, "max_tokens": 1024},
        )
        assert r.status_code == 200
        assert r.json()["params"] == {
            "model_id": HAIKU, "temperature": 0.4, "max_tokens": 1024,
        }

        # Model outside platform ∩ template → 422, params untouched.
        r = await ac.patch(
            f"/api/v1/chat/sessions/{session.id}", json={"model_id": SONNET}
        )
        assert r.status_code == 422
        assert "not allowed" in r.json()["detail"]

        # Rename-only PATCH still works and leaves params alone.
        r = await ac.patch(f"/api/v1/chat/sessions/{session.id}", json={"title": "Renamed"})
        assert r.status_code == 200
        assert r.json()["title"] == "Renamed"
        assert r.json()["params"]["temperature"] == 0.4

    async with audit_db() as adb:
        rows = (
            (await adb.execute(select(AuditLog).where(AuditLog.action == "session_updated")))
            .scalars()
            .all()
        )
        assert any((r.detail or {}).get("params", {}).get("temperature") == 0.4 for r in rows)


# ------------------------------------------------------------- flag semantics


async def test_feature_flag_toggle_merge_and_validation(
    db_session, seeded_row, admin_user, client_for, audit_db
):
    await seeded_row()
    async with client_for(admin_user) as ac:
        current = (await ac.get("/api/v1/admin/model-controls")).json()
        assert current["feature_flags"] == {}

        payload = {
            "model_allowlist": current["model_allowlist"],
            "param_bounds": current["param_bounds"],
            "rate_limits": current["rate_limits"],
            "cost": current["cost"],
            "feature_flags": {"studio_enabled": True},
        }
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 200
        assert r.json()["feature_flags"]["studio_enabled"] is True

        # Merge semantics: a later PUT naming OTHER flags keeps studio_enabled.
        payload["feature_flags"] = {"beta_banner": False}
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.json()["feature_flags"] == {"studio_enabled": True, "beta_banner": False}

        # Omitting feature_flags entirely leaves them untouched.
        del payload["feature_flags"]
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.json()["feature_flags"]["studio_enabled"] is True

        # Non-boolean values refused.
        payload["feature_flags"] = {"studio_enabled": "yes"}
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 422


async def test_meta_features_shapes(
    db_session, seeded_row, test_user, admin_user, business_user, client_for
):
    await seeded_row()
    # Flag off: power user sees nothing; admin sees dark preview.
    async with client_for(test_user) as ac:
        body = (await ac.get("/api/v1/meta/features")).json()
        assert body == {"studio_enabled": False, "studio": False, "studio_dark": False, "connectors_enabled": False}
    async with client_for(admin_user) as ac:
        body = (await ac.get("/api/v1/meta/features")).json()
        assert body == {"studio_enabled": False, "studio": True, "studio_dark": True, "connectors_enabled": False}

    await _flag_studio(db_session, True)
    async with client_for(test_user) as ac:
        body = (await ac.get("/api/v1/meta/features")).json()
        assert body == {"studio_enabled": True, "studio": True, "studio_dark": False, "connectors_enabled": False}
    async with client_for(business_user) as ac:  # D17: wizard persona, never studio
        body = (await ac.get("/api/v1/meta/features")).json()
        assert body["studio"] is False and body["studio_dark"] is False


async def test_business_persona_admin_keeps_dark_access(
    db_session, seeded_row, client_for
):
    """Live S17 drill finding: the demo admin carries persona=business.
    Admin role must always reach the dark preview [R1.2] — persona blocks
    apply to non-admins only [D17]."""
    from app.models import User as UserModel

    await seeded_row()
    biz_admin = UserModel(
        cognito_sub="sub-bizadmin", email="bizadmin@marshal.demo",
        role="admin", persona="business", onboarding_completed=True,
    )
    db_session.add(biz_admin)
    await db_session.commit()
    await db_session.refresh(biz_admin)
    session = await _mk_session(db_session, biz_admin)
    async with client_for(biz_admin) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
        assert r.status_code == 200
        body = (await ac.get("/api/v1/meta/features")).json()
        assert body == {"studio_enabled": False, "studio": True, "studio_dark": True, "connectors_enabled": False}


async def test_resolve_session_call_no_params_matches_defaults(
    db_session, seeded_row, audit_db
):
    """Sessions predating S18 (params NULL) resolve exactly as before."""
    await seeded_row()
    plan = await resolve_session_call(None, None)
    assert plan.model_id == SONNET  # platform default chat model
    assert plan.temperature is None  # nothing sent → model default
    assert plan.requested == {"model_id": None, "temperature": None, "max_tokens": None}
    assert plan.clamped_by == {"model_id": None, "temperature": None, "max_tokens": None}


async def test_session_uuid_not_found(seeded_row, test_user, client_for, db_session):
    await seeded_row(feature_flags={"studio_enabled": True})
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{uuid.uuid4()}/effective-params")
        assert r.status_code == 404


# ------------------------------------- brownfield substrate (spec R1/R2.4)


async def test_substrate_attach_replace_remove_roundtrip(
    db_session, seeded_row, test_user, business_user, client_for, audit_db
):
    await seeded_row()
    session = await _mk_session(db_session, test_user)

    async with client_for(test_user) as ac:
        # Attach
        r = await ac.put(
            f"/api/v1/chat/sessions/{session.id}/substrate",
            json={"content": "SYSTEM: existing support bot", "source": "prod v3"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["has_substrate"] is True
        assert body["substrate_source"] == "prod v3"
        assert body["substrate_size"] == len("SYSTEM: existing support bot")

        # Replace
        r = await ac.put(
            f"/api/v1/chat/sessions/{session.id}/substrate",
            json={"content": "SYSTEM: v4 with tools"},
        )
        assert r.status_code == 200
        assert r.json()["substrate_source"] is None

        # Session GET view carries presence, never content
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}")
        assert r.json()["has_substrate"] is True
        assert "substrate" not in {k for k in r.json() if k == "substrate"}

        # Oversize refused with a named 422 (spec R1.2)
        r = await ac.put(
            f"/api/v1/chat/sessions/{session.id}/substrate",
            json={"content": "y" * 200_001},
        )
        assert r.status_code == 422

        # Remove
        r = await ac.delete(f"/api/v1/chat/sessions/{session.id}/substrate")
        assert r.status_code == 200
        assert r.json()["has_substrate"] is False

    # Outsider (no project, not creator): 404 via _owned_session
    async with client_for(business_user) as ac:
        r = await ac.put(
            f"/api/v1/chat/sessions/{session.id}/substrate",
            json={"content": "nope"},
        )
        assert r.status_code == 404


async def test_prompt_context_shows_substrate_segment(
    db_session, seeded_row, test_user, client_for
):
    await seeded_row(feature_flags={"studio_enabled": True})
    session = await _mk_session(db_session, test_user)
    session.substrate = "SYSTEM: existing claims triage agent"
    session.substrate_source = "claims-bot"
    await db_session.commit()

    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
    assert r.status_code == 200
    body = r.json()
    labels = [s["label"] for s in body["segments"]]
    assert "Brownfield substrate" in labels
    joined = " ".join(s["content"] for s in body["segments"])
    assert "existing claims triage agent" in joined
    assert "claims-bot" in joined

    # Remove → segment gone (spec AC-1)
    session.substrate = None
    session.substrate_source = None
    await db_session.commit()
    async with client_for(test_user) as ac:
        r = await ac.get(f"/api/v1/chat/sessions/{session.id}/prompt-context")
    assert "Brownfield substrate" not in [s["label"] for s in r.json()["segments"]]


# ------------------------- substrate document upload (pdf-docx-ingestion R1.3)


def _substrate_pdf_b64(text: str = "SYSTEM: legacy claims bot") -> str:
    """In-test single-page PDF (the test_marketplace fixture shape)."""
    import base64

    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj",
        b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream
        + b"\nendstream endobj",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objs:
        offsets.append(len(out))
        out += obj + b"\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    return base64.b64encode(bytes(out)).decode()


async def test_substrate_document_attach_pdf(
    db_session, seeded_row, test_user, client_for, audit_db
):
    await seeded_row()
    session = await _mk_session(db_session, test_user)
    async with client_for(test_user) as ac:
        r = await ac.put(
            f"/api/v1/chat/sessions/{session.id}/substrate",
            json={"document_b64": _substrate_pdf_b64(), "source": "claims-bot.pdf"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["has_substrate"] is True
        assert body["substrate_source"] == "claims-bot.pdf"
        # substrate_size = EXTRACTED character count (text-at-rest semantics)
        assert body["substrate_size"] == len("SYSTEM: legacy claims bot")


async def test_substrate_document_validation(
    db_session, seeded_row, test_user, client_for, audit_db
):
    import base64
    import io
    import zipfile

    await seeded_row()
    session = await _mk_session(db_session, test_user)
    url = f"/api/v1/chat/sessions/{session.id}/substrate"
    async with client_for(test_user) as ac:
        # XOR: both members
        r = await ac.put(
            url, json={"content": "text", "document_b64": _substrate_pdf_b64()}
        )
        assert r.status_code == 422
        assert "content or document_b64" in r.json()["detail"]
        # XOR: neither member
        r = await ac.put(url, json={"source": "just a label"})
        assert r.status_code == 422
        # Archives are refused by name (a spec set is a project, not substrate)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("requirements.md", "# R")
        r = await ac.put(
            url, json={"document_b64": base64.b64encode(buf.getvalue()).decode()}
        )
        assert r.status_code == 422
        assert "not an archive" in r.json()["detail"]
        # UTF-8 text through the document path is accepted as-is
        r = await ac.put(
            url,
            json={"document_b64": base64.b64encode(b"plain notes").decode()},
        )
        assert r.status_code == 200
        assert r.json()["substrate_size"] == len("plain notes")


# --------------------------------------------------- stream failure messaging


async def _stream_failure_events(
    db_session, seeded_row, test_user, client_for, monkeypatch, *, exc, partial: str
):
    """Drive /messages with a stream that yields `partial` then raises `exc`;
    return (parsed error event, messages the route persisted)."""
    import json

    from app.services.bedrock import StreamResult

    await seeded_row(feature_flags={"studio_enabled": True})
    session = await _mk_session(db_session, test_user)
    store: list[dict] = []

    async def put_message(session_id, role, content, **kwargs):
        store.append({"role": role, "content": content, **kwargs})

    async def get_messages(session_id):
        return []

    async def failing_stream(**kwargs):
        result: StreamResult = kwargs["result"]
        if partial:
            result.text_parts.append(partial)
            yield partial
        raise exc

    async def no_preflight(*args, **kwargs):
        return None

    monkeypatch.setattr(chat_service, "put_message", put_message)
    monkeypatch.setattr(chat_service, "get_messages", get_messages)
    monkeypatch.setattr("app.api.chat.stream_converse", failing_stream)
    monkeypatch.setattr("app.api.chat.preflight_model_call", no_preflight)

    async with client_for(test_user) as ac:
        r = await ac.post(f"/api/v1/chat/sessions/{session.id}/messages", json={"content": "hi"})
        assert r.status_code == 200
        body = r.text
    error_line = next(
        line for line in body.splitlines() if line.startswith("data:") and '"code"' in line
    )
    return json.loads(error_line.removeprefix("data:").strip()), store


async def test_mid_stream_stall_keeps_partial_and_names_it(
    db_session, seeded_row, test_user, client_for, monkeypatch
):
    """Wave-1 review finding 4: a ReadTimeoutError AFTER tokens flowed is a
    stall, not a generic failure — the user is told the partial reply was kept."""
    from botocore.exceptions import ReadTimeoutError

    from app.api.chat import STREAM_STALL_MESSAGE

    event, store = await _stream_failure_events(
        db_session, seeded_row, test_user, client_for, monkeypatch,
        exc=ReadTimeoutError(endpoint_url="https://bedrock-runtime.example"),
        partial="The first half of the answer",
    )
    assert event["code"] == "ReadTimeoutError"
    assert event["message"] == STREAM_STALL_MESSAGE
    assert "partial answer was kept" in event["message"]
    kept = [m for m in store if m["role"] == "assistant"]
    assert kept and kept[0]["content"] == "The first half of the answer"
    assert kept[0]["truncated"] is True


async def test_stream_timeout_before_first_token_stays_generic(
    db_session, seeded_row, test_user, client_for, monkeypatch
):
    """No text yet → nothing was kept, so the stall wording would be false;
    the generic failure message is still used (and nothing is persisted)."""
    from botocore.exceptions import ReadTimeoutError

    event, store = await _stream_failure_events(
        db_session, seeded_row, test_user, client_for, monkeypatch,
        exc=ReadTimeoutError(endpoint_url="https://bedrock-runtime.example"),
        partial="",
    )
    assert event["code"] == "ReadTimeoutError"
    assert event["message"] == "The model request failed. Please try again."
    assert [m for m in store if m["role"] == "assistant"] == []
