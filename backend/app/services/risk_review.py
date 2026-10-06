"""Risk review workflow (FSD §4.6.3/§4.6.4/§4.6.7, risk-review-workflow spec).

Routing: medium → managers, high → governance_board; empty groups skip forward
through the chain with a configuration alert. Escalation derives purely from
persisted timestamps on an idempotent tick — restarts are harmless. The S4
deploy-gate contract is unchanged; this module changes who answers and how.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiRiskAssessment, Alert, Project, ReviewerGroupMember, User

logger = logging.getLogger("marshal.risk_review")

CHAIN = ["managers", "governance_board", "admins"]  # §4.6.7 escalation order
LEVEL_ROUTE = {"medium": "managers", "high": "governance_board"}
GROUP_LABELS = {
    "managers": "Managers",
    "governance_board": "AI Governance Board",
    "admins": "Platform Admins",
}
ESCALATE_AFTER = timedelta(hours=48)
ADMIN_ALERT_AFTER = timedelta(hours=72)
VALID_GROUPS = ("managers", "governance_board")

OUTCOME_TO_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
}


class ReviewError(Exception):
    """Invalid decision/membership operation — surfaces as 422."""


async def group_member_ids(db: AsyncSession, group_name: str) -> list[uuid.UUID]:
    if group_name == "admins":
        rows = await db.execute(select(User.id).where(User.role == "admin"))
    else:
        rows = await db.execute(
            select(ReviewerGroupMember.user_id).where(
                ReviewerGroupMember.group_name == group_name
            )
        )
    return [r[0] for r in rows.all()]


async def _first_nonempty_group(db: AsyncSession, start: str) -> tuple[str, list[str]]:
    """Walk the chain from `start`; return (group, skipped_groups)."""
    skipped: list[str] = []
    index = CHAIN.index(start)
    for group in CHAIN[index:]:
        members = await group_member_ids(db, group)
        if members:
            return group, skipped
        skipped.append(group)
    return "admins", skipped  # admins always terminal even if empty (nowhere else to go)


async def route(db: AsyncSession, assessment: AiRiskAssessment) -> None:
    """Assign a pending assessment to its reviewer group + notify (R1)."""
    if assessment.decision != "pending" or assessment.assigned_group is not None:
        return
    start = LEVEL_ROUTE.get(assessment.level or "medium", "managers")
    group, skipped = await _first_nonempty_group(db, start)
    assessment.assigned_group = group
    assessment.routed_at = datetime.now(UTC)
    await db.commit()

    if skipped:
        # Configuration gap: intended group(s) empty (R1.2)
        db.add(
            Alert(
                kind="risk_config",
                severity="warning",
                message=(
                    f"Risk review routed past empty group(s) {', '.join(skipped)} "
                    f"to {group} — configure reviewers under Admin → Risk → Reviewers."
                ),
                dedupe_key=f"risk_config:{':'.join(skipped)}:{datetime.now(UTC):%Y-%m-%d}",
            )
        )
        try:
            await db.commit()
        except Exception:  # noqa: BLE001 — dedupe or race; alert is best-effort
            await db.rollback()
            # Rollback EXPIRES loaded ORM state (even with expire_on_commit=False);
            # any later attribute access would lazy-load → MissingGreenlet in async
            # SQLAlchemy. Observed live when the daily risk_config dedupe collided
            # (second routing past an empty group on the same day) — refresh
            # restores the row so routing/notification proceed normally.
            await db.refresh(assessment)

    await _notify_group(db, assessment, group, resubmission=assessment.resubmission_of is not None)


async def _notify_group(
    db: AsyncSession, assessment: AiRiskAssessment, group: str, *, resubmission: bool = False,
    escalated: bool = False,
) -> None:
    from app.services import notifications as notif

    project = await db.get(Project, assessment.project_id)
    name = project.name if project else "a project"
    prefix = "Escalated: " if escalated else ("Resubmission: " if resubmission else "")
    await notif.notify_many(
        db,
        await group_member_ids(db, group),
        type="risk_escalated" if escalated else "risk_review_requested",
        title=f"{prefix}risk review needed — {name}",
        body=(
            f'"{name}" scored {assessment.score}/100 ({assessment.level}) and needs '
            f"{GROUP_LABELS.get(group, group)} review before it can deploy."
        ),
        link="/reviews",
        dedupe_key=f"review_req:{assessment.id}:{group}",
    )


async def eligible_deciders(db: AsyncSession, assessment: AiRiskAssessment) -> set[uuid.UUID]:
    """Assigned group ∪ escalated group ∪ admins (rights widen, never narrow)."""
    ids: set[uuid.UUID] = set(await group_member_ids(db, "admins"))
    if assessment.assigned_group:
        ids.update(await group_member_ids(db, assessment.assigned_group))
    if assessment.escalated_to:
        ids.update(await group_member_ids(db, assessment.escalated_to))
    return ids


async def can_decide(db: AsyncSession, assessment: AiRiskAssessment, user: User) -> bool:
    if user.role == "admin":
        return True
    return user.id in await eligible_deciders(db, assessment)


async def decide(
    db: AsyncSession,
    assessment: AiRiskAssessment,
    actor: User,
    *,
    outcome: str,
    notes: str | None,
) -> AiRiskAssessment:
    """Three-outcome decision (R2.3); terminal per assessment (R2.5)."""
    if outcome not in OUTCOME_TO_DECISION:
        raise ReviewError(f"Unknown outcome '{outcome}'")
    if assessment.decision != "pending":
        raise ReviewError(
            f"Assessment is '{assessment.decision}' — only pending assessments can be decided"
        )
    if outcome in ("reject", "request_changes") and not (notes or "").strip():
        raise ReviewError("This outcome requires notes explaining the decision")

    assessment.decision = OUTCOME_TO_DECISION[outcome]
    assessment.decided_by = actor.id
    assessment.decided_at = datetime.now(UTC)
    assessment.notes = (notes or "").strip() or None
    await db.commit()
    await db.refresh(assessment)

    # Owner notification per outcome (R2.4)
    from app.services import notifications as notif

    project = await db.get(Project, assessment.project_id)
    if project is not None:
        owner = await db.get(User, project.user_id)
        if owner is not None:
            if outcome == "request_changes":
                event, title = "risk_changes_requested", f"Changes requested — {project.name}"
                body = f"Reviewer notes: {assessment.notes}"
            else:
                event = "risk_decided"
                title = f"Risk review {assessment.decision} — {project.name}"
                body = assessment.notes or (
                    "You can deploy now." if outcome == "approve" else "See reviewer notes."
                )
            await notif.notify(
                db, owner, type=event, title=title, body=body,
                link=f"/projects/{project.id}",
                dedupe_key=f"decision:{assessment.id}",
            )
    return assessment


async def add_owner_comment(
    db: AsyncSession, assessment: AiRiskAssessment, owner: User, body: str
) -> AiRiskAssessment:
    """One reply per changes-request (R3.4); full threads are S7 scope."""
    if assessment.decision != "changes_requested":
        raise ReviewError("Comments are for changes-requested assessments")
    if assessment.owner_comment:
        raise ReviewError("A comment was already added; revise the spec to continue")
    assessment.owner_comment = body.strip()[:2000]
    assessment.owner_comment_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(assessment)
    # Surface to the deciding reviewer (best effort)
    if assessment.decided_by:
        from app.services import notifications as notif

        reviewer = await db.get(User, assessment.decided_by)
        project = await db.get(Project, assessment.project_id)
        if reviewer is not None:
            await notif.notify(
                db, reviewer, type="risk_review_requested",
                title=f"Owner replied — {project.name if project else 'project'}",
                body=assessment.owner_comment,
                link="/reviews",
                dedupe_key=f"owner_comment:{assessment.id}",
            )
    return assessment


async def link_resubmission(db: AsyncSession, assessment: AiRiskAssessment) -> None:
    """Called by risk.ensure_assessment on new pending rows: link the latest
    changes_requested predecessor (R3.3)."""
    predecessor = (
        await db.execute(
            select(AiRiskAssessment)
            .where(
                AiRiskAssessment.project_id == assessment.project_id,
                AiRiskAssessment.decision == "changes_requested",
                AiRiskAssessment.id != assessment.id,
            )
            .order_by(AiRiskAssessment.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if predecessor is not None:
        assessment.resubmission_of = predecessor.id
        await db.commit()


def _next_group(current: str | None) -> str | None:
    if current is None:
        return None
    index = CHAIN.index(current)
    return CHAIN[index + 1] if index + 1 < len(CHAIN) else None


async def escalation_tick() -> None:
    """Idempotent, timestamp-derived escalation sweep (R4; §4.6.7)."""
    from app.core.db import SessionLocal

    now = datetime.now(UTC)
    async with SessionLocal() as db:
        # 48h: escalate one level (once — escalated_at guards)
        stale = (
            await db.execute(
                select(AiRiskAssessment).where(
                    AiRiskAssessment.decision == "pending",
                    AiRiskAssessment.routed_at.is_not(None),
                    AiRiskAssessment.routed_at < now - ESCALATE_AFTER,
                    AiRiskAssessment.escalated_at.is_(None),
                )
            )
        ).scalars().all()
        for assessment in stale:
            next_group = _next_group(assessment.assigned_group)
            if next_group is None:
                continue  # already at admins — the 72h alert handles it
            group, _skipped = await _first_nonempty_group(db, next_group)
            assessment.escalated_to = group
            assessment.escalated_at = now
            await db.commit()
            await _notify_group(db, assessment, group, escalated=True)
            logger.info("assessment %s escalated to %s", assessment.id, group)

        # 72h: critical alert to platform admins (once — admin_alerted_at guards)
        overdue = (
            await db.execute(
                select(AiRiskAssessment).where(
                    AiRiskAssessment.decision == "pending",
                    AiRiskAssessment.routed_at.is_not(None),
                    AiRiskAssessment.routed_at < now - ADMIN_ALERT_AFTER,
                    AiRiskAssessment.admin_alerted_at.is_(None),
                )
            )
        ).scalars().all()
        for assessment in overdue:
            project = await db.get(Project, assessment.project_id)
            name = project.name if project else str(assessment.project_id)
            db.add(
                Alert(
                    kind="risk_overdue",
                    severity="critical",
                    scope_project_id=assessment.project_id,
                    message=f'Risk review for "{name}" pending >72h — no reviewer decision.',
                    dedupe_key=f"risk_overdue:{assessment.id}",
                )
            )
            assessment.admin_alerted_at = now
            try:
                await db.commit()
            except Exception:  # noqa: BLE001 — dedupe backstop
                await db.rollback()
                continue
            from app.services import notifications as notif

            await notif.notify_many(
                db, await group_member_ids(db, "admins"),
                type="risk_escalated",
                title=f"Review overdue — {name}",
                body="Pending more than 72 hours. FSD §4.6.7 escalation exhausted.",
                link="/reviews",
                dedupe_key=f"risk_overdue_notif:{assessment.id}",
            )


def _live_project_only(stmt):
    """Exclude assessments whose project has been deleted.

    A deleted project cannot be deployed, so asking a reviewer to decide on one
    is pure noise — and the queue counter it inflates makes the review workload
    look worse than it is. Found on the live platform: 25 pending reviews, all
    but one belonging to deleted drill projects.
    """
    return stmt.where(
        AiRiskAssessment.project_id.in_(
            select(Project.id).where(Project.status != "deleted")
        )
    )


async def queue_for_user(db: AsyncSession, user: User) -> list[AiRiskAssessment]:
    """Pending assessments the user may decide (R2.2)."""
    if user.role == "admin":
        stmt = select(AiRiskAssessment).where(AiRiskAssessment.decision == "pending")
    else:
        memberships = [
            r[0]
            for r in (
                await db.execute(
                    select(ReviewerGroupMember.group_name).where(
                        ReviewerGroupMember.user_id == user.id
                    )
                )
            ).all()
        ]
        if not memberships:
            return []
        stmt = select(AiRiskAssessment).where(
            AiRiskAssessment.decision == "pending",
            (AiRiskAssessment.assigned_group.in_(memberships))
            | (AiRiskAssessment.escalated_to.in_(memberships)),
        )
    result = await db.execute(
        _live_project_only(stmt).order_by(AiRiskAssessment.routed_at.asc())
    )
    return list(result.scalars())


async def user_group_memberships(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    rows = await db.execute(
        select(ReviewerGroupMember.group_name).where(ReviewerGroupMember.user_id == user_id)
    )
    return [r[0] for r in rows.all()]


async def set_group_members(
    db: AsyncSession, group_name: str, user_ids: list[uuid.UUID], acting: User
) -> list[uuid.UUID]:
    if group_name not in VALID_GROUPS:
        raise ReviewError(f"Unknown reviewer group '{group_name}'")
    existing = (
        await db.execute(
            select(ReviewerGroupMember).where(ReviewerGroupMember.group_name == group_name)
        )
    ).scalars().all()
    wanted = set(user_ids)
    for member in existing:
        if member.user_id not in wanted:
            await db.delete(member)
    current = {m.user_id for m in existing}
    for user_id in wanted - current:
        user = await db.get(User, user_id)
        if user is None or user.status == "suspended":
            raise ReviewError(f"User {user_id} not found or suspended")
        db.add(
            ReviewerGroupMember(group_name=group_name, user_id=user_id, added_by=acting.id)
        )
    await db.commit()
    return list(wanted)


async def summary_counters(db: AsyncSession) -> dict:
    pending_by_group_rows = await db.execute(
        _live_project_only(
            select(AiRiskAssessment.assigned_group, func.count()).where(
                AiRiskAssessment.decision == "pending"
            )
        ).group_by(AiRiskAssessment.assigned_group)
    )
    since = datetime.now(UTC) - timedelta(days=30)
    decided = (
        await db.execute(
            select(AiRiskAssessment.routed_at, AiRiskAssessment.decided_at).where(
                AiRiskAssessment.decided_at.is_not(None),
                AiRiskAssessment.decided_at >= since,
                AiRiskAssessment.routed_at.is_not(None),
            )
        )
    ).all()
    durations = sorted(
        (decided_at - routed_at).total_seconds() / 3600
        for routed_at, decided_at in decided
        if routed_at and decided_at
    )
    median_h = durations[len(durations) // 2] if durations else None
    escalations = (
        await db.execute(
            select(func.count()).select_from(AiRiskAssessment).where(
                AiRiskAssessment.escalated_at.is_not(None),
                AiRiskAssessment.escalated_at >= since,
            )
        )
    ).scalar_one()
    return {
        "pending_by_group": {g or "unrouted": c for g, c in pending_by_group_rows.all()},
        "median_decision_hours_30d": round(median_h, 1) if median_h is not None else None,
        "escalations_30d": escalations,
    }
