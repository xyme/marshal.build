"""Reviewer-facing review APIs (risk-review-workflow spec R2/R3/R6).

Reviewers need NOT be admins: group membership grants queue access and
decision rights on assessments routed (or escalated) to their groups.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin_security_if_admin
from app.core.db import get_db
from app.models import AiRiskAssessment, Project, User
from app.services import audit
from app.services import risk_review as svc

router = APIRouter(tags=["reviews"])


class DecideIn(BaseModel):
    outcome: str = Field(pattern="^(approve|reject|request_changes)$")
    notes: str | None = Field(default=None, max_length=2000)


class CommentIn(BaseModel):
    body: str = Field(min_length=3, max_length=2000)


async def _assessment_payload(db: AsyncSession, assessment: AiRiskAssessment) -> dict:
    project = await db.get(Project, assessment.project_id)
    owner = await db.get(User, project.user_id) if project else None
    waiting_h = None
    if assessment.routed_at and assessment.decision == "pending":
        routed = assessment.routed_at
        if routed.tzinfo is None:  # sqlite returns naive datetimes
            routed = routed.replace(tzinfo=UTC)
        waiting_h = round((datetime.now(UTC) - routed).total_seconds() / 3600, 1)
    # B13/B21: reviewers see the effective posture + whether it came from the
    # safe default, explicit key requirement, or explicit public opt-out.
    from app.models import Spec
    from app.services.codegen.validate import endpoint_auth_decision

    latest_req = (
        await db.execute(
            select(Spec.content)
            .where(Spec.project_id == assessment.project_id, Spec.type == "requirements")
            .order_by(Spec.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    auth = endpoint_auth_decision(latest_req or "")
    return {
        "id": str(assessment.id),
        "project_id": str(assessment.project_id),
        "project_name": project.name if project else None,
        "owner_email": owner.email if owner else None,
        "endpoint_auth": auth["mode"],
        "endpoint_auth_source": auth["source"],
        "score": assessment.score,
        "level": assessment.level,
        "factors": assessment.factors,
        "decision": assessment.decision,
        "notes": assessment.notes,
        "assigned_group": assessment.assigned_group,
        "escalated_to": assessment.escalated_to,
        "routed_at": assessment.routed_at.isoformat() if assessment.routed_at else None,
        "waiting_hours": waiting_h,
        "resubmission_of": str(assessment.resubmission_of) if assessment.resubmission_of else None,
        "owner_comment": assessment.owner_comment,
        "created_at": assessment.created_at.isoformat(),
    }


@router.get("/reviews")
async def list_reviews(
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    groups = await svc.user_group_memberships(db, user.id)
    items = await svc.queue_for_user(db, user)
    return {
        "groups": groups,
        "is_reviewer": bool(groups) or user.role == "admin",
        "items": [await _assessment_payload(db, a) for a in items],
    }


@router.get("/reviews/meta")
async def reviews_meta(
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    groups = await svc.user_group_memberships(db, user.id)
    pending = len(await svc.queue_for_user(db, user)) if (groups or user.role == "admin") else 0
    return {"is_reviewer": bool(groups) or user.role == "admin", "pending": pending}


@router.post("/risk-assessments/{assessment_id}/decide")
async def decide_assessment(
    assessment_id: uuid.UUID,
    payload: DecideIn,
    request: Request,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    assessment = await db.get(AiRiskAssessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if not await svc.can_decide(db, assessment, user):
        raise HTTPException(
            status_code=403, detail="You are not a reviewer for this assessment"
        )
    try:
        assessment = await svc.decide(
            db, assessment, user, outcome=payload.outcome, notes=payload.notes
        )
    except svc.ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(
        request,
        project_id=str(assessment.project_id),
        outcome=payload.outcome,
        score=assessment.score,
        level=assessment.level,
    )
    return await _assessment_payload(db, assessment)


@router.post("/risk-assessments/{assessment_id}/comment")
async def owner_comment(
    assessment_id: uuid.UUID,
    payload: CommentIn,
    request: Request,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    assessment = await db.get(AiRiskAssessment, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    project = await db.get(Project, assessment.project_id)
    if project is None or (project.user_id != user.id and user.role != "admin"):
        raise HTTPException(status_code=403, detail="Only the project owner may comment")
    try:
        assessment = await svc.add_owner_comment(db, assessment, user, payload.body)
    except svc.ReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, project_id=str(assessment.project_id))
    return await _assessment_payload(db, assessment)
