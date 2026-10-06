"""Marketplace submission tests (marketplace-submissions spec)."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models import MarketplaceSample, Notification, Project, Spec, User

pytestmark = pytest.mark.asyncio


async def _mk_user(db, *, email: str, role="power", persona="power") -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        persona=persona,
        onboarding_completed=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _project_with_spec(db, owner: User, *, content="# Requirements\nReal content") -> Project:
    project = Project(user_id=owner.id, name="Submittable", status="spec_complete")
    db.add(project)
    await db.flush()
    db.add(
        Spec(project_id=project.id, version=1, type="requirements", content=content, created_by=owner.id)
    )
    await db.commit()
    await db.refresh(project)
    return project


SUBMIT_BODY = {
    "title": "Invoice Assistant",
    "summary": "Extracts and validates invoice data for finance teams.",
    "category": "document_processing",
    "keywords": ["invoices", "finance"],
}


async def test_submit_snapshot_and_conflicts(db_session, test_user, client_for, audit_db):
    owner = test_user  # power persona
    project = await _project_with_spec(db_session, owner)
    admin = await _mk_user(db_session, email="mkadmin@marshal.demo", role="admin")

    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 201, r.text
        submission = r.json()
        assert submission["status"] == "submitted"

        # one open submission per project
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 409

    # snapshot immutability: later edits don't touch the submission
    sample = await db_session.get(MarketplaceSample, uuid.UUID(submission["id"]))
    original = sample.spec_snapshot["requirements_md"]
    db_session.add(
        Spec(project_id=project.id, version=2, type="requirements", content="# CHANGED", created_by=owner.id)
    )
    await db_session.commit()
    await db_session.refresh(sample)
    assert sample.spec_snapshot["requirements_md"] == original

    # admins were notified
    await asyncio.sleep(0.05)
    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == admin.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(n.type == "submission_received" for n in rows)


async def test_submit_authz(db_session, test_user, client_for):
    owner = test_user
    project = await _project_with_spec(db_session, owner)
    business = await _mk_user(
        db_session, email="biz@marshal.demo", role="business", persona="business"
    )
    biz_project = await _project_with_spec(db_session, business)

    # business persona owner → 403 (power-user surface)
    async with client_for(business) as client:
        r = await client.post(
            f"/api/v1/projects/{biz_project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 403

    # power user who is NOT the owner → 404 (not disclosed)
    async with client_for(await _mk_user(db_session, email="np@marshal.demo")) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 404

    # empty project → 422
    empty = Project(user_id=owner.id, name="Empty", status="draft")
    db_session.add(empty)
    await db_session.commit()
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{empty.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 422


async def test_review_state_machine(db_session, test_user, client_for, audit_db):
    owner = test_user
    admin = await _mk_user(db_session, email="curator@marshal.demo", role="admin")
    project = await _project_with_spec(db_session, owner)

    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        first_id = r.json()["id"]

    async with client_for(admin) as client:
        # queue shows it
        r = await client.get("/api/v1/admin/marketplace/submissions")
        assert [s["id"] for s in r.json()] == [first_id]
        # reject requires feedback (schema-enforced)
        r = await client.post(f"/api/v1/admin/marketplace/submissions/{first_id}/reject", json={"feedback": ""})
        assert r.status_code == 422
        r = await client.post(
            f"/api/v1/admin/marketplace/submissions/{first_id}/reject",
            json={"feedback": "Needs a design document before we can list it."},
        )
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        await asyncio.sleep(0.05)  # drain notify tasks (shared sqlite connection)
        # terminal: decide-twice refused
        r = await client.post(
            f"/api/v1/admin/marketplace/submissions/{first_id}/approve"
        )
        assert r.status_code == 422

    # author sees feedback in history + can resubmit with lineage
    async with client_for(owner) as client:
        r = await client.get("/api/v1/marketplace/my-submissions")
        mine = r.json()
        assert mine[0]["review_feedback"].startswith("Needs a design")
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 201
        second = r.json()
        assert second["resubmission_of"] == first_id

    await asyncio.sleep(0.05)  # drain notify tasks before the next decision
    # approve → draft in the existing curation path; author notified
    async with client_for(admin) as client:
        r = await client.post(
            f"/api/v1/admin/marketplace/submissions/{second['id']}/approve"
        )
        assert r.status_code == 200 and r.json()["status"] == "draft"
    await asyncio.sleep(0.05)
    types = [
        n.type
        for n in (
            await db_session.execute(
                select(Notification).where(Notification.user_id == owner.id)
            )
        ).scalars()
    ]
    assert types.count("submission_decided") == 2


async def test_withdraw_rules(db_session, test_user, client_for):
    owner = test_user
    other = await _mk_user(db_session, email="somebody@marshal.demo")
    project = await _project_with_spec(db_session, owner)
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        sid = r.json()["id"]
    # only the author may withdraw
    async with client_for(other) as client:
        r = await client.post(f"/api/v1/marketplace/submissions/{sid}/withdraw")
        assert r.status_code == 404
    async with client_for(owner) as client:
        r = await client.post(f"/api/v1/marketplace/submissions/{sid}/withdraw")
        assert r.status_code == 200 and r.json()["status"] == "withdrawn"
        r = await client.post(f"/api/v1/marketplace/submissions/{sid}/withdraw")
        assert r.status_code == 422
        # resubmission after withdrawal allowed
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        assert r.status_code == 201 and r.json()["resubmission_of"] == sid


async def test_non_published_never_leaks_publicly(db_session, test_user, client_for):
    owner = test_user
    project = await _project_with_spec(db_session, owner)
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        sid = r.json()["id"]
        # public catalog: absent
        r = await client.get("/api/v1/marketplace/samples")
        assert all(s["id"] != sid for s in r.json()["items"])
        # public detail: 404 for non-admin (even the author)
        r = await client.get(f"/api/v1/marketplace/samples/{sid}")
        assert r.status_code == 404


async def test_published_submission_carries_contributor_credit(
    db_session, test_user, client_for
):
    owner = test_user
    admin = await _mk_user(db_session, email="pub@marshal.demo", role="admin")
    project = await _project_with_spec(db_session, owner)
    async with client_for(owner) as client:
        r = await client.post(
            f"/api/v1/projects/{project.id}/submit-to-marketplace", json=SUBMIT_BODY
        )
        sid = r.json()["id"]
    async with client_for(admin) as client:
        r = await client.post(f"/api/v1/admin/marketplace/submissions/{sid}/approve")
        assert r.status_code == 200
        # curation completes the required fields via existing update path
        sample = await db_session.get(MarketplaceSample, uuid.UUID(sid))
        snapshot = dict(sample.spec_snapshot)
        snapshot["design_md"] = "# Design\nfilled"
        snapshot["tasks_md"] = "# Tasks\n- [ ] 1"
        r = await client.put(
            f"/api/v1/admin/marketplace/samples/{sid}",
            json={
                "title": sample.title,
                "description": sample.description,
                "category": sample.category,
                "complexity": "beginner",
                "models_used": [],
                "spec_snapshot": snapshot,
                "keywords": ["invoices"],
            },
        )
        assert r.status_code == 200, r.text
        r = await client.post(f"/api/v1/admin/marketplace/samples/{sid}/publish")
        assert r.status_code == 200, r.text

    async with client_for(owner) as client:
        r = await client.get("/api/v1/marketplace/samples")
        card = next(s for s in r.json()["items"] if s["id"] == sid)
        assert card["contributed_by"] == owner.name
        r = await client.get(f"/api/v1/marketplace/samples/{sid}")
        assert r.json()["contributed_by"] == owner.name
