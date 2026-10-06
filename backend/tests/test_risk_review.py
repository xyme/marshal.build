"""Risk review workflow: routing, authz, outcomes, escalation (S6 spec)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AiRiskAssessment,
    Alert,
    Notification,
    Project,
    ReviewerGroupMember,
    Spec,
)
from app.services import risk as risk_svc
from app.services import risk_review as svc

pytestmark = pytest.mark.asyncio


async def _mk_assessment(
    db, user, *, level="medium", decision="pending", routed_hours_ago=None,
    assigned_group=None, name="Reviewed",
) -> AiRiskAssessment:
    project = Project(user_id=user.id, name=name, status="spec_complete")
    db.add(project)
    await db.flush()
    db.add(Spec(project_id=project.id, version=1, type="requirements", content="# r"))
    assessment = AiRiskAssessment(
        project_id=project.id,
        content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        rubric_version=risk_svc.RUBRIC_VERSION,
        score=45 if level == "medium" else 70,
        level=level,
        factors={},
        status="scored",
        decision=decision,
        assigned_group=assigned_group,
        routed_at=(
            datetime.now(UTC) - timedelta(hours=routed_hours_ago)
            if routed_hours_ago is not None
            else None
        ),
    )
    db.add(assessment)
    await db.commit()
    await db.refresh(assessment)
    return assessment


async def _add_member(db, group, user, admin):
    db.add(ReviewerGroupMember(group_name=group, user_id=user.id, added_by=admin.id))
    await db.commit()


# --------------------------------------------------------------------- routing


async def test_route_medium_to_managers(db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "managers", business_user, admin_user)
    assessment = await _mk_assessment(db_session, test_user, level="medium")
    await svc.route(db_session, assessment)
    assert assessment.assigned_group == "managers"
    assert assessment.routed_at is not None
    notif_users = set(
        (await db_session.execute(select(Notification.user_id))).scalars().all()
    )
    assert business_user.id in notif_users  # AC-6 notification


async def test_route_high_to_board(db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "governance_board", business_user, admin_user)
    assessment = await _mk_assessment(db_session, test_user, level="high")
    await svc.route(db_session, assessment)
    assert assessment.assigned_group == "governance_board"


async def test_route_empty_group_skips_forward_with_alert(db_session, test_user, admin_user, audit_db):
    # No managers, no board → chain lands on admins + config alert
    assessment = await _mk_assessment(db_session, test_user, level="medium")
    await svc.route(db_session, assessment)
    assert assessment.assigned_group == "admins"
    kinds = (await db_session.execute(select(Alert.kind))).scalars().all()
    assert "risk_config" in kinds


async def test_route_empty_group_twice_same_day_survives_dedupe(
    db_session, test_user, admin_user, audit_db
):
    """Regression (live S8 drill): the SECOND empty-group route on a day hits
    the risk_config alert dedupe → IntegrityError → rollback expired the
    assessment ORM row → MissingGreenlet on the next attribute access → 500.
    Routing must survive the dedupe collision and still assign + notify."""
    first = await _mk_assessment(db_session, test_user, level="medium")
    await svc.route(db_session, first)
    second = await _mk_assessment(db_session, test_user, level="medium")
    await svc.route(db_session, second)  # alert dedupe collides here
    assert second.assigned_group == "admins"
    assert second.routed_at is not None
    kinds = (await db_session.execute(select(Alert.kind))).scalars().all()
    assert kinds.count("risk_config") == 1  # deduped, not duplicated


async def test_route_is_idempotent(db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "managers", business_user, admin_user)
    assessment = await _mk_assessment(db_session, test_user)
    await svc.route(db_session, assessment)
    first_routed = assessment.routed_at
    await svc.route(db_session, assessment)  # no re-route
    assert assessment.routed_at == first_routed


# ----------------------------------------------------------------------- authz


async def test_decide_authz_matrix(db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "managers", business_user, admin_user)
    assessment = await _mk_assessment(db_session, test_user, assigned_group="managers")

    assert await svc.can_decide(db_session, assessment, business_user) is True  # group member
    assert await svc.can_decide(db_session, assessment, admin_user) is True  # admin always
    assert await svc.can_decide(db_session, assessment, test_user) is False  # owner ≠ reviewer

    # Board member cannot decide a managers-routed assessment
    board_only = await _mk_assessment(db_session, test_user, assigned_group="governance_board", name="B")
    assert await svc.can_decide(db_session, board_only, business_user) is False


async def test_escalation_widens_decision_rights(db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "governance_board", business_user, admin_user)
    assessment = await _mk_assessment(
        db_session, test_user, assigned_group="managers", routed_hours_ago=1
    )
    assert await svc.can_decide(db_session, assessment, business_user) is False
    assessment.escalated_to = "governance_board"
    await db_session.commit()
    assert await svc.can_decide(db_session, assessment, business_user) is True


# --------------------------------------------------------------------- outcomes


async def test_three_outcomes_and_notes_rules(db_session, test_user, admin_user, audit_db):
    a1 = await _mk_assessment(db_session, test_user, assigned_group="admins", name="A1")
    approved = await svc.decide(db_session, a1, admin_user, outcome="approve", notes=None)
    assert approved.decision == "approved"

    a2 = await _mk_assessment(db_session, test_user, assigned_group="admins", name="A2")
    with pytest.raises(svc.ReviewError, match="notes"):
        await svc.decide(db_session, a2, admin_user, outcome="reject", notes="")
    rejected = await svc.decide(db_session, a2, admin_user, outcome="reject", notes="nope")
    assert rejected.decision == "rejected"

    a3 = await _mk_assessment(db_session, test_user, assigned_group="admins", name="A3")
    changes = await svc.decide(
        db_session, a3, admin_user, outcome="request_changes", notes="tighten data handling"
    )
    assert changes.decision == "changes_requested"

    # Terminal: no double-decide
    with pytest.raises(svc.ReviewError, match="pending"):
        await svc.decide(db_session, a3, admin_user, outcome="approve", notes=None)

    # Owner notified per outcome
    types = set((await db_session.execute(select(Notification.type))).scalars().all())
    assert "risk_decided" in types and "risk_changes_requested" in types


async def test_owner_comment_once(db_session, test_user, admin_user, audit_db):
    assessment = await _mk_assessment(db_session, test_user, assigned_group="admins")
    await svc.decide(db_session, assessment, admin_user, outcome="request_changes", notes="fix X")
    commented = await svc.add_owner_comment(db_session, assessment, test_user, "X is intentional because Y")
    assert commented.owner_comment.startswith("X is intentional")
    with pytest.raises(svc.ReviewError, match="already"):
        await svc.add_owner_comment(db_session, assessment, test_user, "another")


async def test_resubmission_linking(db_session, test_user, admin_user, audit_db):
    first = await _mk_assessment(db_session, test_user, assigned_group="admins")
    await svc.decide(db_session, first, admin_user, outcome="request_changes", notes="fix")
    # New assessment on the SAME project (revised content)
    second = AiRiskAssessment(
        project_id=first.project_id, content_hash=uuid.uuid4().hex * 2,
        rubric_version=risk_svc.RUBRIC_VERSION, score=40, level="medium", factors={},
        status="scored", decision="pending",
    )
    db_session.add(second)
    await db_session.commit()
    await db_session.refresh(second)
    await svc.link_resubmission(db_session, second)
    assert second.resubmission_of == first.id


# ------------------------------------------------------------------ escalation


async def test_escalation_tick_matrix(db_session, test_user, business_user, admin_user, audit_db, monkeypatch):
    await _add_member(db_session, "governance_board", business_user, admin_user)
    fresh = await _mk_assessment(
        db_session, test_user, assigned_group="managers", routed_hours_ago=47, name="Fresh"
    )
    stale = await _mk_assessment(
        db_session, test_user, assigned_group="managers", routed_hours_ago=49, name="Stale"
    )
    overdue = await _mk_assessment(
        db_session, test_user, assigned_group="managers", routed_hours_ago=73, name="Overdue"
    )

    await svc.escalation_tick()
    await db_session.refresh(fresh)
    await db_session.refresh(stale)
    await db_session.refresh(overdue)
    assert fresh.escalated_at is None  # 47h — untouched
    assert stale.escalated_to == "governance_board" and stale.escalated_at is not None
    assert overdue.escalated_at is not None and overdue.admin_alerted_at is not None

    kinds = (await db_session.execute(select(Alert.kind))).scalars().all()
    assert kinds.count("risk_overdue") == 1

    # Idempotence: second tick changes nothing
    first_escalated_at = stale.escalated_at
    await svc.escalation_tick()
    await db_session.refresh(stale)
    await db_session.refresh(overdue)
    assert stale.escalated_at == first_escalated_at
    kinds = (await db_session.execute(select(Alert.kind))).scalars().all()
    assert kinds.count("risk_overdue") == 1  # still once


# ------------------------------------------------------------------------ APIs


async def test_reviews_queue_scoping(client, client_for, db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "managers", business_user, admin_user)
    await _mk_assessment(db_session, test_user, assigned_group="managers", routed_hours_ago=2, name="Q1")
    await _mk_assessment(db_session, test_user, assigned_group="governance_board", routed_hours_ago=2, name="Q2")

    async with client_for(business_user) as ac:
        body = (await ac.get("/api/v1/reviews")).json()
        assert body["is_reviewer"] is True and body["groups"] == ["managers"]
        assert [i["project_name"] for i in body["items"]] == ["Q1"]
        assert body["items"][0]["waiting_hours"] >= 2

    # Non-reviewer (owner) sees empty reviewer surface
    queue = (await client.get("/api/v1/reviews")).json()
    assert queue["is_reviewer"] is False and queue["items"] == []

    # Admin sees everything
    async with client_for(admin_user) as ac:
        body = (await ac.get("/api/v1/reviews")).json()
        assert len(body["items"]) == 2


async def test_reviewer_decide_endpoint_authz(client, client_for, db_session, test_user, business_user, admin_user, audit_db):
    await _add_member(db_session, "managers", business_user, admin_user)
    assessment = await _mk_assessment(db_session, test_user, assigned_group="managers")

    # Owner (power user, not a reviewer) → 403
    own = await client.post(
        f"/api/v1/risk-assessments/{assessment.id}/decide",
        json={"outcome": "approve"},
    )
    assert own.status_code == 403

    async with client_for(business_user) as ac:
        ok = await ac.post(
            f"/api/v1/risk-assessments/{assessment.id}/decide",
            json={"outcome": "request_changes", "notes": "narrow the data scope"},
        )
        assert ok.status_code == 200
        assert ok.json()["decision"] == "changes_requested"


async def test_owner_comment_endpoint(client, client_for, db_session, test_user, admin_user, audit_db):
    assessment = await _mk_assessment(db_session, test_user, assigned_group="admins")
    async with client_for(admin_user) as ac:
        await ac.post(
            f"/api/v1/risk-assessments/{assessment.id}/decide",
            json={"outcome": "request_changes", "notes": "fix"},
        )
    posted = await client.post(
        f"/api/v1/risk-assessments/{assessment.id}/comment", json={"body": "clarified in v2"}
    )
    assert posted.status_code == 200 and posted.json()["owner_comment"] == "clarified in v2"


async def test_reviewer_groups_crud_and_export(client, client_for, db_session, test_user, business_user, admin_user, audit_db):
    async with client_for(admin_user) as ac:
        put = await ac.put(
            "/api/v1/admin/reviewer-groups/managers",
            json={"user_ids": [str(business_user.id)]},
        )
        assert put.status_code == 200
        groups = (await ac.get("/api/v1/admin/reviewer-groups")).json()
        assert groups["groups"]["managers"][0]["email"] == business_user.email
        bad = await ac.put(
            "/api/v1/admin/reviewer-groups/nonsense", json={"user_ids": []}
        )
        assert bad.status_code == 422

        assessment = await _mk_assessment(db_session, test_user, assigned_group="admins")
        await ac.post(
            f"/api/v1/risk-assessments/{assessment.id}/decide", json={"outcome": "approve"}
        )
        export = await ac.get("/api/v1/admin/risk-assessments/export")
        assert export.status_code == 200
        assert "approved" in export.text

    # Role gate: non-admin cannot manage groups
    put = await client.put(
        "/api/v1/admin/reviewer-groups/managers", json={"user_ids": []}
    )
    assert put.status_code == 403


async def test_gate_changes_requested_mapping(client, db_session, test_user, admin_user, audit_db):
    from app.services import risk as risk_svc

    assessment = await _mk_assessment(db_session, test_user, assigned_group="admins", name="GateCR")
    # Gate matches on CURRENT content hash — align the row with the real docs
    assessment.content_hash = risk_svc.content_hash({"requirements": "# r"})
    await db_session.commit()
    await svc.decide(db_session, assessment, admin_user, outcome="request_changes", notes="do X")
    project = await db_session.get(Project, assessment.project_id)
    with pytest.raises(risk_svc.GateBlocked) as exc:
        await risk_svc.deployment_gate(db_session, project, test_user)
    assert exc.value.code == "risk_changes_requested"
    assert "do X" in exc.value.payload["notes"]


async def test_queue_excludes_deleted_projects(db_session, admin_user, test_user, audit_db):
    """A deleted project cannot deploy, so its pending review is not work.

    Found live: 25 pending reviews, all but one belonging to deleted drill
    projects — the queue counter made the workload look far worse than it was.
    """
    from app.models import AiRiskAssessment, Project
    from app.services import risk_review

    live = Project(user_id=test_user.id, name="Live project", status="spec_complete")
    gone = Project(user_id=test_user.id, name="Deleted project", status="deleted")
    db_session.add_all([live, gone])
    await db_session.commit()

    for project, content_hash in ((live, "hash-live"), (gone, "hash-gone")):
        db_session.add(
            AiRiskAssessment(
                project_id=project.id, content_hash=content_hash, rubric_version=risk_svc.RUBRIC_VERSION,
                score=72, level="high", status="scored", decision="pending",
                assigned_group="governance_board", routed_at=datetime.now(UTC),
            )
        )
    await db_session.commit()

    queue = await risk_review.queue_for_user(db_session, admin_user)
    project_ids = {a.project_id for a in queue}
    assert live.id in project_ids
    assert gone.id not in project_ids

    counters = await risk_review.summary_counters(db_session)
    assert counters["pending_by_group"].get("governance_board") == 1
