"""B17 configurable risk rubrics: policy resolution, versioned determinism,
gate strictness across policy changes, admin API + audit."""

import json
from unittest.mock import patch

import pytest

from app.models import PlatformSettings, Project, Spec
from app.services import risk as risk_svc

pytestmark = pytest.mark.asyncio

GOOD_RUBRIC = json.dumps({
    "data_sensitivity": {"score": 2, "rationale": "Generic internal docs."},
    "user_facing": {"score": 2, "rationale": "Internal tool."},
    "request_volume": {"score": 2, "rationale": "Ad-hoc usage."},
})
# defaults: 0.3*2 + 0.2*2(capability haiku/sonnet->advanced? computed) ... the
# fixture allowlist resolves standard tier -> capability 5; scope 3.
# Rubric v2 (13.5R) added composition_reach at 0.10; rubric v3
# (agent-substance) added capability_reach at 0.10 — both score 0 for a
# plain dependency-free, rung-free project. Default arithmetic:
# total = (0.3*2 + 0.2*5 + 0.2*2 + 0.15*3 + 0.15*2 + 0.10*0 + 0.10*0)/1.20*10
# = 23 -> LOW under defaults (<=30); MEDIUM once band_low_max drops below 23.


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    from app.services import platform_settings as settings_svc

    settings_svc.invalidate_cache()
    yield
    settings_svc.invalidate_cache()


@pytest.fixture(autouse=True)
async def seeded_platform_row(db_session):
    row = PlatformSettings(
        id=1,
        model_allowlist=["us.anthropic.claude-sonnet-5"],
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


async def _project_with_docs(db, user, name="Policy") -> Project:
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


def _fake_converse(rubric_json: str, calls: list | None = None, systems: list | None = None):
    async def fake(*, messages, system, model_id, max_tokens=None, temperature=None,
                   top_p=None, ctx=None):
        if calls is not None:
            calls.append(1)
        if systems is not None:
            systems.append(system)
        return rubric_json, {"inputTokens": 10, "outputTokens": 10}, "end_turn"

    return fake


async def _set_policy(db, **payload):
    return await risk_svc.update_risk_policy(db, payload)


# ------------------------------------------------- resolver + validation


async def test_resolver_defaults_when_blob_absent(db_session):
    policy = await risk_svc.resolve_risk_policy(db_session)
    assert policy.version == 1
    assert policy.auto_approve_low is True
    assert policy.band_low_max == 30 and policy.band_medium_max == 60
    assert policy.weights == risk_svc.WEIGHTS
    assert policy.anchors == {}


async def test_weights_are_normalized_proportions(db_session):
    """Admins enter proportions; effective weights normalize (R1.3)."""
    await _set_policy(db_session, weights={
        "data_sensitivity": 30, "model_capability": 20, "user_facing": 20,
        "deployment_scope": 15, "request_volume": 15,
    })
    policy = await risk_svc.resolve_risk_policy(db_session)
    total = sum(risk_svc.WEIGHTS.values())  # 1.20 since rubric v3 (agent-substance)
    assert policy.normalized_weights() == pytest.approx(
        {k: v / total for k, v in risk_svc.WEIGHTS.items()}
    )
    # proportionally identical input ⇒ effective policy unchanged ⇒ NO bump
    assert policy.version == 1


async def test_validation_matrix(db_session):
    for bad in (
        {"auto_approve_low": "yes"},
        {"band_low_max": 0},
        {"band_medium_max": 100},
        {"band_low_max": 60, "band_medium_max": 40},
        {"weights": {"data_sensitivity": 1}},
        {"weights": {**risk_svc.WEIGHTS, "data_sensitivity": 0}},
        {"anchors": {"model_capability": "computed factors have no anchors"}},
        {"anchors": {"data_sensitivity": ""}},
        {"anchors": {"data_sensitivity": "x" * 601}},
    ):
        with pytest.raises(risk_svc.RiskPolicyError):
            await risk_svc.update_risk_policy(db_session, bad)


async def test_version_bumps_once_per_effective_change(db_session):
    p1 = await _set_policy(db_session, band_low_max=25)
    assert p1.version == 2
    p2 = await _set_policy(db_session, band_low_max=25)  # no-op
    assert p2.version == 2, "a no-op PUT must not bump (R2.1)"
    p3 = await _set_policy(db_session, auto_approve_low=False)
    assert p3.version == 3


# ------------------------------------------------- scoring under policy


async def test_custom_thresholds_reband_and_hold_the_gate(db_session, test_user):
    """Same content scores LOW under defaults, MEDIUM once the low band
    tightens below its score — and the gate flips from pass to hold."""
    project = await _project_with_docs(db_session, test_user)
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        first = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
        assert first.level == "low" and first.decision == "auto_approved"
        assert first.policy_version == 1
        gate_ok = await risk_svc.deployment_gate(db_session, project, test_user)
        assert gate_ok.id == first.id

        await _set_policy(db_session, band_low_max=20)  # 23 > 20 -> medium now

        with pytest.raises(risk_svc.GateBlocked) as blocked:
            await risk_svc.deployment_gate(db_session, project, test_user)
        assert blocked.value.code == "risk_pending"
        fresh = await risk_svc.latest_assessment(db_session, project.id)
        assert fresh.id != first.id
        assert fresh.policy_version == 2
        assert fresh.level == "medium" and fresh.decision == "pending"


async def test_auto_approve_off_queues_low(db_session, test_user):
    await _set_policy(db_session, auto_approve_low=False)
    project = await _project_with_docs(db_session, test_user, name="QueueLow")
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        row = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert row.level == "low"
    assert row.decision == "pending", "auto-approve off means EVERYTHING queues (R1.1)"


async def test_same_policy_version_is_a_cache_hit(db_session, test_user):
    calls: list = []
    project = await _project_with_docs(db_session, test_user, name="CacheHit")
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC, calls)):
        a = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
        b = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert a.id == b.id and len(calls) == 1


async def test_policy_change_rescores_and_supersedes_human_approval(
    db_session, test_user, admin_user
):
    """R2.4: an approval under v1 neither opens nor poisons the gate after a
    policy change — the fresh row starts its own lifecycle."""
    await _set_policy(db_session, auto_approve_low=False)  # v2: everything queues
    project = await _project_with_docs(db_session, test_user, name="Supersede")
    calls: list = []
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC, calls)):
        first = await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
        assert first.decision == "pending"
        await risk_svc.decide(db_session, first, admin_user, approve=True, notes="ok")
        assert (await risk_svc.deployment_gate(db_session, project, test_user)).id == first.id

        await _set_policy(db_session, band_low_max=10)  # v3: tighten

        with pytest.raises(risk_svc.GateBlocked):
            await risk_svc.deployment_gate(db_session, project, test_user)
        fresh = await risk_svc.latest_assessment(db_session, project.id)
        assert fresh.policy_version == 3 and fresh.decision == "pending"
        assert len(calls) == 2, "one re-score per policy version, no more"


async def test_anchor_override_lands_in_the_prompt(db_session, test_user):
    systems: list = []
    override = "0 nothing; 10 sovereign wealth fund ledgers."
    await _set_policy(db_session, anchors={"data_sensitivity": override})
    project = await _project_with_docs(db_session, test_user, name="Anchors")
    with patch.object(
        risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC, systems=systems)
    ):
        await risk_svc.ensure_assessment(db_session, project, user_id=test_user.id)
    assert systems and override in systems[0]
    assert risk_svc.DEFAULT_ANCHORS["user_facing"] in systems[0], (
        "non-overridden factors keep the platform anchor"
    )


# ------------------------------------------------- admin API


async def test_policy_api_roundtrip_and_validation(
    client, client_for, admin_user, audit_db
):
    async with client_for(admin_user) as ac:
        r = await ac.get("/api/v1/admin/governance/risk-policy")
        assert r.status_code == 200
        body = r.json()
        assert body["policy_version"] == 1 and body["is_default"] is True
        assert body["default_anchors"]["data_sensitivity"]

        r = await ac.put(
            "/api/v1/admin/governance/risk-policy", json={"band_low_max": 25}
        )
        assert r.status_code == 200
        assert r.json()["policy_version"] == 2

        r = await ac.put(
            "/api/v1/admin/governance/risk-policy",
            json={"band_low_max": 90, "band_medium_max": 40},
        )
        assert r.status_code == 422
        assert "band_low_max" in r.json()["detail"]

    # non-admin refused
    r = await client.put(
        "/api/v1/admin/governance/risk-policy", json={"band_low_max": 26}
    )
    assert r.status_code in (401, 403)


# ------------------------- agent-substance R3.1: capability_reach (rubric v3)


def test_capability_reach_score_table():
    from app.services.capabilities import capability_reach_score

    assert capability_reach_score("# plain")[0] == 0
    assert capability_reach_score("SHALL keep conversation memory")[0] == 2
    assert capability_reach_score("SHALL use packaged dependencies")[0] == 3
    assert capability_reach_score("SHALL use tools: convert_units")[0] == 5
    assert capability_reach_score("SHALL plan multi-step responses")[0] == 5
    both = "SHALL use tools: a_tool\nSHALL use packaged dependencies"
    assert capability_reach_score(both)[0] == 6
    loaded = both + "\nSHALL keep conversation memory"
    score, rationale = capability_reach_score(loaded)
    assert score == 7 and "memory" in rationale


async def test_capability_reach_moves_the_score(db_session, test_user):
    """AC-5: the risk score moves when rungs are declared, per policy weights."""
    plain = await _project_with_docs(db_session, test_user, name="Plain")
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        base = await risk_svc.ensure_assessment(db_session, plain, user_id=test_user.id)

    rungy = await _project_with_docs(db_session, test_user, name="Rungy")
    db_session.add(
        Spec(
            project_id=rungy.id, version=2, type="requirements",
            content="# Reqs\nThe agent SHALL use tools: convert_units.\n"
            "It SHALL keep conversation memory.\n",
        )
    )
    await db_session.commit()
    with patch.object(risk_svc, "converse", side_effect=_fake_converse(GOOD_RUBRIC)):
        rung_row = await risk_svc.ensure_assessment(db_session, rungy, user_id=test_user.id)

    assert rung_row.rubric_version == risk_svc.RUBRIC_VERSION == 3
    factor = rung_row.factors["capability_reach"]
    assert factor["score"] == 6 and "tools" in factor["rationale"]
    # 2.75 base vs 2.75 + 6*0.10 = 3.35 → /1.20*10: 23 vs 28
    assert base.score == 23 and rung_row.score == 28
    assert rung_row.score > base.score
