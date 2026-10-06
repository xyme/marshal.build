"""Guided mode state machine + spec export (guided-mode / alpha-polish specs)."""

import io
import json
import zipfile
from unittest.mock import patch

import pytest

from app.models import Project, Spec
from app.services import chat as chat_service
from app.services import export as export_svc
from app.services import guided as guided_svc

pytestmark = pytest.mark.asyncio

USE_CASE = {"problem": "Answer HR policy questions for our staff automatically.",
            "category": "chatbot", "category_other": None}
CONTEXT = {"audience": "internal", "data_sources": ["documents"], "sensitive_data": "none"}
BEHAVIOR = {"key_actions": "Answer questions with citations; escalate to HR when unsure.",
            "constraints": "English only"}

QUESTIONS_JSON = json.dumps({"questions": ["Which document formats?", "How many users?"]})
PROCEED_JSON = json.dumps({"proceed": True})


def _fake_clarify(response_text: str):
    async def fake(*, messages, system, model_id, max_tokens=None, temperature=None,
                   top_p=None, ctx=None):
        return response_text, {}, "end_turn"

    return fake


@pytest.fixture(autouse=True)
def default_controls():
    from app.services.platform_settings import _DEFAULTS

    async def controls():
        return _DEFAULTS

    with patch("app.services.platform_settings.get_controls", side_effect=controls):
        yield


@pytest.fixture(autouse=True)
def no_dynamo(monkeypatch):
    """Chat history in-memory: guided put_message/get_messages without DynamoDB."""
    store: dict[str, list[dict]] = {}

    async def put_message(session_id, role, content, **kwargs):
        store.setdefault(str(session_id), []).append(
            {"role": role, "content": content, "sk": str(len(store.get(str(session_id), [])))}
        )

    async def get_messages(session_id):
        return list(store.get(str(session_id), []))

    monkeypatch.setattr(chat_service, "put_message", put_message)
    monkeypatch.setattr(chat_service, "get_messages", get_messages)
    return store


# ------------------------------------------------------------- session modes


async def test_business_forced_guided_and_freeform_403(db_session, business_user):
    session = await chat_service.create_session(db_session, business_user)
    assert session.mode == "guided" and session.guided_state["step"] == "use_case"
    with pytest.raises(chat_service.PersonaModeError):
        await chat_service.create_session(db_session, business_user, mode="freeform")


async def test_power_defaults_freeform_may_choose_guided(db_session, test_user):
    default = await chat_service.create_session(db_session, test_user)
    assert default.mode == "freeform" and default.guided_state is None
    opted = await chat_service.create_session(db_session, test_user, mode="guided")
    assert opted.mode == "guided"


async def test_switch_freeform_power_only(db_session, test_user, business_user):
    session = await chat_service.create_session(db_session, business_user)
    with pytest.raises(guided_svc.GuidedStateError, match="Power User"):
        await guided_svc.switch_to_freeform(db_session, session, business_user)
    own = await chat_service.create_session(db_session, test_user, mode="guided")
    own.guided_state = guided_svc.advance_step(own, "use_case", USE_CASE)
    await db_session.commit()
    switched = await guided_svc.switch_to_freeform(db_session, own, test_user)
    assert switched.mode == "freeform"
    history = await chat_service.get_messages(own.id)
    assert any("guided wizard" in m["content"] for m in history)


# ------------------------------------------------------------- state machine


async def test_step_machine_with_back_nav(db_session, business_user):
    session = await chat_service.create_session(db_session, business_user)
    state = guided_svc.advance_step(session, "use_case", USE_CASE)
    session.guided_state = state
    assert state["step"] == "context"
    state = guided_svc.advance_step(session, "context", CONTEXT)
    session.guided_state = state
    assert state["step"] == "behavior"
    # Back-nav: revise use_case while at behavior — step stays at behavior
    revised = {**USE_CASE, "problem": "Answer HR and IT policy questions for staff."}
    state = guided_svc.advance_step(session, "use_case", revised)
    session.guided_state = state
    assert state["step"] == "behavior"
    assert state["answers"]["use_case"]["problem"].startswith("Answer HR and IT")
    # Cannot skip ahead was enforced earlier; finish forward
    state = guided_svc.advance_step(session, "behavior", BEHAVIOR)
    session.guided_state = state
    assert state["step"] == "clarify"
    # Form steps freeze once clarification starts
    with pytest.raises(guided_svc.GuidedStateError, match="frozen"):
        guided_svc.advance_step(session, "use_case", USE_CASE)


async def test_validation_errors_are_specific(db_session, business_user):
    session = await chat_service.create_session(db_session, business_user)
    with pytest.raises(guided_svc.GuidedStateError, match="use_case.problem"):
        guided_svc.advance_step(session, "use_case", {"problem": "short", "category": "chatbot"})
    with pytest.raises(guided_svc.GuidedStateError, match="other"):
        guided_svc.advance_step(session, "use_case", {**USE_CASE, "category": "other"})
    session.guided_state = guided_svc.advance_step(session, "use_case", USE_CASE)
    with pytest.raises(guided_svc.GuidedStateError, match="Unknown data sources"):
        guided_svc.advance_step(session, "context", {**CONTEXT, "data_sources": ["telepathy"]})


async def _wizard_to_clarify(db_session, user):
    session = await chat_service.create_session(db_session, user, mode="guided")
    for step, answers in (("use_case", USE_CASE), ("context", CONTEXT), ("behavior", BEHAVIOR)):
        session.guided_state = guided_svc.advance_step(session, step, answers)
    await db_session.commit()
    await db_session.refresh(session)
    return session


async def test_clarify_rounds_answers_and_model_satisfied(db_session, test_user):
    session = await _wizard_to_clarify(db_session, test_user)
    with patch.object(guided_svc, "converse", side_effect=_fake_clarify(QUESTIONS_JSON)):
        state = await guided_svc.run_clarify_round(db_session, session, test_user)
    assert state["clarification"]["rounds"] == 1
    assert state["clarification"]["questions_total"] == 2
    session.guided_state = guided_svc.record_clarify_answers(
        session,
        [{"question_id": "q1", "answer": "PDF and Word documents"},
         {"question_id": "q2", "skip": True}],
    )
    await db_session.commit()
    with patch.object(guided_svc, "converse", side_effect=_fake_clarify(PROCEED_JSON)):
        state = await guided_svc.run_clarify_round(db_session, session, test_user)
    assert state["step"] == "generate"
    assert state["outcome"] == "completed"
    # Skipped question became an assumption
    assert any("How many users?" in a for a in state["assumptions"])


async def test_clarify_caps_force_proceed(db_session, test_user):
    session = await _wizard_to_clarify(db_session, test_user)
    five = json.dumps({"questions": [f"Q{i}?" for i in range(5)]})
    with patch.object(guided_svc, "converse", side_effect=_fake_clarify(five)):
        for _ in range(3):  # 3 rounds × 5 questions = 15 = cap
            await db_session.refresh(session)
            state = await guided_svc.run_clarify_round(db_session, session, test_user)
        assert state["clarification"]["rounds"] == 3
        await db_session.refresh(session)
        state = await guided_svc.run_clarify_round(db_session, session, test_user)
    assert state["step"] == "generate"  # caps reached → forced proceed
    assert state["outcome"] == "proceeded_with_assumptions"
    assert state["clarification"]["questions_total"] == 15


async def test_clarify_failure_falls_through_to_generate(db_session, test_user):
    session = await _wizard_to_clarify(db_session, test_user)

    async def broken(**kwargs):
        return "absolute garbage", {}, "end_turn"

    with patch.object(guided_svc, "converse", side_effect=broken):
        state = await guided_svc.run_clarify_round(db_session, session, test_user)
    assert state["step"] == "generate"  # never strands the wizard (R2.4)


async def test_brief_is_deterministic_and_complete(db_session, test_user):
    session = await _wizard_to_clarify(db_session, test_user)
    with patch.object(guided_svc, "converse", side_effect=_fake_clarify(QUESTIONS_JSON)):
        await guided_svc.run_clarify_round(db_session, session, test_user)
    session.guided_state = guided_svc.record_clarify_answers(
        session, [{"question_id": "q1", "answer": "PDF only"}, {"question_id": "q2", "skip": True}]
    )
    await guided_svc.freeze_assumptions(db_session, session, reason="user_proceeded")
    await db_session.refresh(session)
    brief_a = guided_svc.build_brief(session.guided_state)
    brief_b = guided_svc.build_brief(session.guided_state)
    assert brief_a == brief_b
    for expected in ("## Problem", "## Category", "## Audience", "## Data & Sensitivity",
                     "## Key Actions", "## Constraints", "## Clarifications",
                     "## Stated Assumptions", "PDF only"):
        assert expected in brief_a
    # prepare_generation persists the brief once (idempotent)
    await guided_svc.prepare_generation(db_session, session, test_user)
    await guided_svc.prepare_generation(db_session, session, test_user)
    history = await chat_service.get_messages(session.id)
    assert len([m for m in history if m["role"] == "user"]) == 1


async def test_guided_api_flow(business_user, client_for, db_session, audit_db):
    async with client_for(business_user) as ac:
        created = (await ac.post("/api/v1/chat/sessions", json={})).json()
        assert created["mode"] == "guided"
        sid = created["id"]
        forced = await ac.post("/api/v1/chat/sessions", json={"mode": "freeform"})
        assert forced.status_code == 422

        step1 = await ac.post(
            f"/api/v1/chat/sessions/{sid}/guided/answer",
            json={"step": "use_case", "answers": USE_CASE},
        )
        assert step1.status_code == 200
        assert step1.json()["guided_state"]["step"] == "context"
        bad = await ac.post(
            f"/api/v1/chat/sessions/{sid}/guided/answer",
            json={"step": "behavior", "answers": BEHAVIOR},
        )
        assert bad.status_code == 422  # cannot skip ahead
        switch = await ac.post(f"/api/v1/chat/sessions/{sid}/guided/switch-freeform")
        assert switch.status_code == 403  # business persona


# -------------------------------------------------------------------- export


async def test_export_zip_layout_and_manifest(client, db_session, test_user, audit_db):
    project = Project(user_id=test_user.id, name="My Cool App!", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    db_session.add(Spec(project_id=project.id, version=1, type="requirements", content="# R v1"))
    db_session.add(Spec(project_id=project.id, version=2, type="requirements", content="# R v2"))
    db_session.add(Spec(project_id=project.id, version=1, type="design", content="# D\n```mermaid\ngraph TD\nA-->B\n```"))
    await db_session.commit()

    resp = await client.get(f"/api/v1/projects/{project.id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    slug = export_svc.project_slug(project)
    assert f".kiro/specs/{slug}/requirements.md" in names
    assert f".kiro/specs/{slug}/design.md" in names
    assert not any(n.endswith("tasks.md") for n in names)
    assert archive.read(f".kiro/specs/{slug}/requirements.md").decode() == "# R v2"  # latest, byte-identical
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["documents"]["requirements"]["version"] == 2
    assert manifest["documents"]["tasks"] == {"absent": True}
    assert manifest["project"]["slug"] == slug
    assert slug.startswith("my-cool-app")


async def test_export_empty_project_409(client, db_session, test_user, audit_db):
    project = Project(user_id=test_user.id, name="Empty", status="draft")
    db_session.add(project)
    await db_session.commit()
    resp = await client.get(f"/api/v1/projects/{project.id}/export")
    assert resp.status_code == 409
