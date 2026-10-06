"""Platform settings, risk engine, deploy gate, costs (cost-risk-governance spec)."""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import (
    AiRiskAssessment,
    ModelInvocation,
    PlatformSettings,
    Project,
    Spec,
)
from app.services import costs as costs_svc
from app.services import platform_settings as settings_svc
from app.services import risk as risk_svc

pytestmark = pytest.mark.asyncio

SONNET = "us.anthropic.claude-sonnet-5"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

GOOD_RUBRIC = json.dumps({
    "data_sensitivity": {"score": 2, "rationale": "Generic internal docs."},
    "user_facing": {"score": 2, "rationale": "Internal tool."},
    "request_volume": {"score": 2, "rationale": "Ad-hoc usage."},
})
HIGH_RUBRIC = json.dumps({
    "data_sensitivity": {"score": 9, "rationale": "Credit and income data."},
    "user_facing": {"score": 8, "rationale": "External customers."},
    "request_volume": {"score": 7, "rationale": "Thousands of requests daily."},
})


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    settings_svc.invalidate_cache()
    yield
    settings_svc.invalidate_cache()


@pytest.fixture(autouse=True)
def seeded_platform_row(db_session):
    """Migration seeds row id=1; tests create it explicitly (create_all path)."""

    async def seed():
        row = PlatformSettings(
            id=1,
            model_allowlist=[SONNET, HAIKU],
            param_bounds={
                "temperature": {"min": 0.0, "max": 1.0},
                "top_p": {"min": 0.0, "max": 1.0},
                "max_tokens": 8192,
                "max_context": 200000,
            },
            rate_limits={}, cost={},
        )
        db_session.add(row)
        await db_session.commit()

    import asyncio

    asyncio.get_event_loop()
    return seed()


async def _mk_project_with_docs(db, user, name="Risky") -> Project:
    project = Project(user_id=user.id, name=name, status="spec_complete")
    db.add(project)
    await db.flush()
    for doc_type, content in (
        ("requirements", "# Reqs\n- FR-1: things"),
        ("design", "# Design\narch"),
        ("tasks", "# Tasks\n- [ ] 1. build"),
    ):
        db.add(Spec(project_id=project.id, version=1, type=doc_type, content=content))
    await db.commit()
    await db.refresh(project)
    return project


def _fake_converse(rubric_json: str):
    async def fake(*, messages, system, model_id, max_tokens=None, temperature=None,
                   top_p=None, ctx=None):
        return rubric_json, {"inputTokens": 10, "outputTokens": 10}, "end_turn"

    return fake


# ---------------------------------------------------------------- settings


async def test_settings_validation_matrix(db_session, seeded_platform_row, admin_user):
    await seeded_platform_row
    with pytest.raises(settings_svc.SettingsValidationError, match="Unknown model"):
        await settings_svc.update_controls(
            db_session, model_allowlist=["nope"], param_bounds={}, rate_limits={},
            cost={}, updated_by=admin_user.id,
        )
    with pytest.raises(settings_svc.SettingsValidationError, match="cannot be empty"):
        await settings_svc.update_controls(
            db_session, model_allowlist=[], param_bounds={}, rate_limits={},
            cost={}, updated_by=admin_user.id,
        )
    with pytest.raises(settings_svc.SettingsValidationError, match="temperature"):
        await settings_svc.update_controls(
            db_session, model_allowlist=[SONNET],
            param_bounds={"temperature": {"min": 0.9, "max": 0.1}},
            rate_limits={}, cost={}, updated_by=admin_user.id,
        )
    row = await settings_svc.update_controls(
        db_session, model_allowlist=[HAIKU],
        param_bounds={"temperature": {"min": 0.0, "max": 0.5}, "max_tokens": 4096},
        rate_limits={"per_user_rpm": 60}, cost={"platform_budget_usd": 300},
        updated_by=admin_user.id,
    )
    assert row.model_allowlist == [HAIKU]


async def test_clamp_helpers():
    controls = settings_svc.ModelControls(
        allowlist=[SONNET], temperature_min=0.2, temperature_max=0.7,
        top_p_min=0.1, top_p_max=0.9, max_tokens=4096, max_context=200000,
    )
    assert settings_svc.clamp_temperature(0.0, controls) == 0.2
    assert settings_svc.clamp_temperature(1.0, controls) == 0.7
    assert settings_svc.clamp_temperature(0.5, controls) == 0.5
    assert settings_svc.clamp_temperature(None, controls) is None
    assert settings_svc.clamp_top_p(0.95, controls) == 0.9


async def test_admin_model_controls_api(admin_user, client_for, db_session, seeded_platform_row, audit_db):
    await seeded_platform_row
    async with client_for(admin_user) as ac:
        current = (await ac.get("/api/v1/admin/model-controls")).json()
        assert SONNET in current["model_allowlist"]
        resp = await ac.put(
            "/api/v1/admin/model-controls",
            json={
                "model_allowlist": [HAIKU],
                "param_bounds": {"temperature": {"min": 0, "max": 1},
                                 "top_p": {"min": 0, "max": 1}, "max_tokens": 2048},
            },
        )
        assert resp.status_code == 200
        assert resp.json()["model_allowlist"] == [HAIKU]
        bad = await ac.put(
            "/api/v1/admin/model-controls",
            json={"model_allowlist": ["bogus"], "param_bounds": {}},
        )
        assert bad.status_code == 422


# -------------------------------------------------------------------- risk


async def test_scoring_deterministic_by_hash(db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)
    calls = {"n": 0}

    async def counting(*, messages, system, model_id, max_tokens=None, temperature=None,
                       top_p=None, ctx=None):
        calls["n"] += 1
        return GOOD_RUBRIC, {}, "end_turn"

    with patch.object(risk_svc, "converse", side_effect=counting):
        first = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
        second = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert first.id == second.id  # hash hit — same stored row (AC-3)
    assert calls["n"] == 1  # LLM consulted exactly once
    assert first.decision == "auto_approved" and first.level == "low"
    # weighted: ds2*.3 + mc5*.2 + uf2*.2 + scope3*.15 + vol2*.15 + reach0 + cap0
    # = 2.75 → /1.20 renormalization (rubric v3, 13.5-agent-substance) *10 → 23
    assert first.score == 23


async def test_content_change_rescores_and_rebands(db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        first = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    db_session.add(Spec(project_id=project.id, version=2, type="requirements",
                        content="# Reqs v2\n- credit scoring for customers"))
    await db_session.commit()
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(HIGH_RUBRIC)):
        second = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert second.id != first.id
    # ds9*.3 + mc5*.2 + uf8*.2 + scope3*.15 + vol7*.15 + reach0 + cap0
    # = 6.8 → /1.20 (rubric v3) *10 → 57: MEDIUM under default bands — the
    # honest v3 dilution; admins who want the old severity tune the bands.
    assert second.score == 57 and second.level == "medium"
    assert second.decision == "pending"


async def test_malformed_rubric_retries_then_error_row(db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)
    attempts = {"n": 0}

    async def broken(*, messages, system, model_id, max_tokens=None, temperature=None,
                     top_p=None, ctx=None):
        attempts["n"] += 1
        return "not json at all", {}, "end_turn"

    with patch.object(risk_svc, "converse", side_effect=broken):
        row = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert attempts["n"] == 2  # one retry
    assert row.status == "error" and row.score is None

    # Error rows are retryable: next ensure re-scores the same content
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        healed = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert healed.status == "scored" and healed.score == 23  # rubric v3 math


async def test_decide_transitions_and_notes_rule(db_session, test_user, admin_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(HIGH_RUBRIC)):
        row = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert row.decision == "pending"
    with pytest.raises(risk_svc.RiskDecisionError, match="notes"):
        await risk_svc.decide(db_session, row, admin_user, approve=False, notes="")
    row = await risk_svc.decide(db_session, row, admin_user, approve=True, notes=None)
    assert row.decision == "approved" and row.decided_by == admin_user.id
    with pytest.raises(risk_svc.RiskDecisionError, match="pending"):
        await risk_svc.decide(db_session, row, admin_user, approve=False, notes="x")


async def test_deploy_gate_matrix(client, db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)

    # LOW → auto-approved → deploy proceeds past the gate (fails later at sandbox
    # config in tests, which surfaces as 409/500 from start_deployment — so patch it)
    async def fake_start(db, user, proj, build=None, mode="full_governance"):
        from app.models import Deployment

        deployment = Deployment(project_id=proj.id, user_id=user.id, status="pending")
        db.add(deployment)
        await db.commit()
        await db.refresh(deployment)
        return deployment

    with (
        patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)),
        patch("app.api.deployments.deploy_service.start_deployment", side_effect=fake_start),
    ):
        ok = await client.post(f"/api/v1/projects/{project.id}/deploy")
        assert ok.status_code == 202

    # Content change → HIGH → pending → 403 risk_pending
    db_session.add(Spec(project_id=project.id, version=2, type="requirements", content="# risky v2"))
    await db_session.commit()
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(HIGH_RUBRIC)):
        blocked = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "risk_pending"

    # Reject → 403 risk_rejected with notes
    assessment = (
        await db_session.execute(
            select(AiRiskAssessment)
            .where(AiRiskAssessment.project_id == project.id)
            .order_by(AiRiskAssessment.created_at.desc()).limit(1)
        )
    ).scalars().first()
    assessment.decision = "rejected"
    assessment.notes = "too risky for alpha"
    await db_session.commit()
    rejected = await client.post(f"/api/v1/projects/{project.id}/deploy")
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "risk_rejected"
    assert "too risky" in rejected.json()["detail"]["notes"]

    # Approve → passes the gate
    assessment.decision = "approved"
    await db_session.commit()
    with patch("app.api.deployments.deploy_service.start_deployment", side_effect=fake_start):
        approved = await client.post(f"/api/v1/projects/{project.id}/deploy")
        assert approved.status_code == 202


async def test_project_risk_endpoint_current_flag(client, db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user)
    none_yet = (await client.get(f"/api/v1/projects/{project.id}/risk")).json()
    # B20 R1.3: the payload always carries mode context alongside the flag
    assert none_yet["assessed"] is False
    assert none_yet["gate_modes"]["full_governance"] == "enforce"
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    fresh = (await client.get(f"/api/v1/projects/{project.id}/risk")).json()
    assert fresh["assessed"] and fresh["current"] and fresh["level"] == "low"
    db_session.add(Spec(project_id=project.id, version=2, type="design", content="changed"))
    await db_session.commit()
    stale = (await client.get(f"/api/v1/projects/{project.id}/risk")).json()
    assert stale["assessed"] and stale["current"] is False


# -------------------------------------------------------------------- costs


async def test_costs_dashboard_and_pivots(db_session, test_user, admin_user, seeded_platform_row, client_for, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user, name="Cost Proj")
    now = datetime.now(UTC)
    for uid, cost in [(test_user.id, "0.10"), (test_user.id, "0.20"), (admin_user.id, "0.30")]:
        db_session.add(
            ModelInvocation(
                user_id=uid, project_id=project.id, purpose="chat", model_id=SONNET,
                prompt_text="p", prompt_sha256="0" * 64, response_text="r",
                input_tokens=100, output_tokens=100, latency_ms=5, cost_usd=cost,
                created_at=now,
            )
        )
    db_session.add(  # unpriced row
        ModelInvocation(
            user_id=test_user.id, project_id=project.id, purpose="chat",
            model_id="unknown.model", prompt_text="p", prompt_sha256="0" * 64,
            response_text="r", input_tokens=1, output_tokens=1, latency_ms=1,
            cost_usd=None, created_at=now,
        )
    )
    await db_session.commit()

    dash = await costs_svc.dashboard(db_session, None)
    assert dash["total_usd"] == pytest.approx(0.6)
    assert dash["calls"] == 4 and dash["unpriced_calls"] == 1
    assert dash["projected_usd"] >= dash["total_usd"]
    assert sum(d["usd"] for d in dash["daily"]) == pytest.approx(0.6)
    assert dash["top_spenders"][0]["usd"] == pytest.approx(0.3)  # admin leads

    by_user = await costs_svc.breakdown(db_session, None, "user")
    assert {i["email"] for i in by_user["items"]} == {test_user.email, admin_user.email}
    by_project = await costs_svc.breakdown(db_session, None, "project")
    assert by_project["items"][0]["name"] == "Cost Proj"
    by_model = await costs_svc.breakdown(db_session, None, "model")
    assert {i["model_id"] for i in by_model["items"]} == {SONNET, "unknown.model"}
    by_day = await costs_svc.breakdown(db_session, None, "day")
    assert by_day["items"][0]["calls"] == 4

    async with client_for(admin_user) as ac:
        api_dash = (await ac.get("/api/v1/admin/costs")).json()
        assert api_dash["total_usd"] == pytest.approx(0.6)
        role_gate = await ac.get("/api/v1/admin/costs")  # admin ok
        assert role_gate.status_code == 200


async def test_costs_role_gate(client, audit_db):
    assert (await client.get("/api/v1/admin/costs")).status_code == 403


async def test_projects_list_carries_risk_and_cost(client, db_session, test_user, seeded_platform_row, audit_db):
    await seeded_platform_row
    project = await _mk_project_with_docs(db_session, test_user, name="Enriched")
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    db_session.add(
        ModelInvocation(
            user_id=test_user.id, project_id=project.id, purpose="chat", model_id=SONNET,
            prompt_text="p", prompt_sha256="0" * 64, response_text="r",
            input_tokens=10, output_tokens=10, latency_ms=1, cost_usd="0.05",
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    body = (await client.get("/api/v1/projects")).json()
    item = next(i for i in body["items"] if i["name"] == "Enriched")
    assert item["risk_level"] == "low"
    assert item["cost_mtd_usd"] == pytest.approx(0.05)
