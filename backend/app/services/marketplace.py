"""Marketplace service (marketplace spec R1–R5).

Search is Postgres ILIKE over title/description + keyword containment — right-
sized for Alpha catalog scale (≤50 samples); OpenSearch/embeddings ≥Beta.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MarketplaceSample, Project, Spec, Template, User
from app.services.guardrails import model_registry
from app.services.templates import VALID_CATEGORIES

logger = logging.getLogger("marshal.marketplace")

SORTS = {
    "popular": (MarketplaceSample.fork_count.desc(), MarketplaceSample.published_at.desc()),
    "newest": (MarketplaceSample.published_at.desc(),),
    "viewed": (MarketplaceSample.view_count.desc(), MarketplaceSample.published_at.desc()),
}
DOC_TYPES = ("requirements", "design", "tasks")


class MarketplaceError(Exception):
    """Curation rule violation — surfaces as 422."""


def _search_filter(stmt, q: str | None):
    if not q:
        return stmt
    like = f"%{q}%"
    return stmt.where(
        or_(
            MarketplaceSample.title.ilike(like),
            MarketplaceSample.description.ilike(like),
            # keywords is a JSON array of strings; cheap containment via text cast
            cast(MarketplaceSample.keywords, String).ilike(like),
        )
    )


async def list_samples(
    db: AsyncSession,
    *,
    q: str | None = None,
    category: str | None = None,
    complexity: str | None = None,
    model: str | None = None,
    sort: str = "popular",
    page: int = 1,
    page_size: int = 12,
    include_all_statuses: bool = False,
    status: str | None = None,
) -> tuple[list[MarketplaceSample], int]:
    from app.core.tenant import tenant_scope

    stmt = tenant_scope(select(MarketplaceSample), MarketplaceSample)
    if include_all_statuses:
        if status:
            stmt = stmt.where(MarketplaceSample.status == status)
    else:
        stmt = stmt.where(MarketplaceSample.status == "published")
    stmt = _search_filter(stmt, q)
    if category:
        stmt = stmt.where(MarketplaceSample.category == category)
    if complexity:
        stmt = stmt.where(MarketplaceSample.complexity == complexity)
    if model:
        stmt = stmt.where(cast(MarketplaceSample.models_used, String).ilike(f"%{model}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    order = SORTS.get(sort, SORTS["popular"])
    result = await db.execute(
        stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size)
    )
    return list(result.scalars()), total


async def category_counts(db: AsyncSession) -> list[tuple[str, int]]:
    result = await db.execute(
        select(MarketplaceSample.category, func.count())
        .where(MarketplaceSample.status == "published")
        .group_by(MarketplaceSample.category)
        .order_by(func.count().desc())
    )
    return list(result.all())


async def get_sample(
    db: AsyncSession, sample_id: uuid.UUID, *, admin: bool = False, count_view: bool = False
) -> MarketplaceSample | None:
    sample = await db.get(MarketplaceSample, sample_id)
    if sample is None:
        return None
    if sample.status != "published" and not admin:
        return None
    if count_view and not admin:
        sample.view_count = (sample.view_count or 0) + 1  # per-request, no dedup (R1.3)
        await db.commit()
        await db.refresh(sample)
    return sample


async def sample_context(db: AsyncSession, sample: MarketplaceSample) -> dict:
    """Author/template display fields for the detail page."""
    author_name = None
    if sample.author_id:
        author = await db.get(User, sample.author_id)
        author_name = (author.name or author.email.split("@")[0]) if author else None
    template_name, template_deprecated = None, False
    if sample.template_id:
        template = await db.get(Template, sample.template_id)
        if template:
            template_name = template.name
            template_deprecated = template.status == "deprecated"
    return {
        "author_name": author_name,
        "template_name": template_name,
        "template_deprecated": template_deprecated,
    }


async def fork_sample(
    db: AsyncSession, sample: MarketplaceSample, user: User, name: str
) -> tuple[Project, list[str]]:
    """Create an independent project + v1 specs from the snapshot (R4).

    Single transaction: project, spec rows, and fork_count move together; any
    failure rolls the whole thing back (no orphans, no phantom counts).
    """
    warnings: list[str] = []
    template = await db.get(Template, sample.template_id) if sample.template_id else None
    if template and template.status == "deprecated":
        warnings.append(
            f'This sample was built with the deprecated template "{template.name}". '
            "Consider migrating to an active template."
        )
    registry_ids = {
        model["id"]
        for model in await model_registry()
        if model.get("selectable", True)
    }
    disallowed = [m for m in (sample.models_used or []) if m not in registry_ids]
    if disallowed:
        warnings.append(
            "Sample references models outside the selectable platform registry: "
            + ", ".join(disallowed)
            + ". Spec validation will flag these on your first edit."
        )

    project = Project(
        user_id=user.id,
        name=name,
        description=sample.description,
        status="draft",
        template_id=sample.template_id,
        origin="marketplace_fork",
        forked_from_sample_id=sample.id,
    )
    db.add(project)
    await db.flush()

    snapshot = sample.spec_snapshot or {}
    has_docs = False
    for doc_type in DOC_TYPES:
        content = snapshot.get(f"{doc_type}_md")
        if content:
            has_docs = True
            db.add(
                Spec(
                    project_id=project.id,
                    version=1,
                    type=doc_type,
                    content=content,
                    origin="forked",
                    created_by=user.id,
                )
            )
    if has_docs:
        project.status = "spec_complete"
    if template:
        template.usage_count = (template.usage_count or 0) + 1
    sample.fork_count = (sample.fork_count or 0) + 1
    await db.commit()
    await db.refresh(project)
    if has_docs:
        # Risk scoring on fork (S4-02 R2.1) — forks arrive with full specs
        from app.services.risk import trigger_assessment

        trigger_assessment(project.id, user.id)
    return project, warnings


# ------------------------------------------------------------------- curation


async def _validate_upsert(payload) -> None:
    if payload.category not in VALID_CATEGORIES:
        raise MarketplaceError(
            f"Unknown category '{payload.category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}"
        )
    registry_ids = {
        model["id"]
        for model in await model_registry()
        if model.get("selectable", True)
    }
    unknown = [model for model in payload.models_used if model not in registry_ids]
    if unknown:
        raise MarketplaceError(
            "models_used contains ids outside the platform registry: " + ", ".join(unknown)
        )


async def create_sample(db: AsyncSession, payload, author: User) -> MarketplaceSample:
    await _validate_upsert(payload)
    sample = MarketplaceSample(**payload.model_dump(), author_id=author.id, status="draft")
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    return sample


async def update_sample(db: AsyncSession, sample: MarketplaceSample, payload) -> MarketplaceSample:
    await _validate_upsert(payload)
    if sample.status == "archived":
        raise MarketplaceError("Archived samples cannot be edited")
    for key, value in payload.model_dump().items():
        setattr(sample, key, value)
    await db.commit()
    await db.refresh(sample)
    return sample


async def import_snapshot(db: AsyncSession, sample: MarketplaceSample, project_id: uuid.UUID) -> dict:
    """Copy a project's latest docs into the snapshot (primary curation path, R5.2)."""
    snapshot = dict(sample.spec_snapshot or {})
    found = 0
    for doc_type in DOC_TYPES:
        result = await db.execute(
            select(Spec)
            .where(Spec.project_id == project_id, Spec.type == doc_type)
            .order_by(Spec.version.desc())
            .limit(1)
        )
        spec = result.scalar_one_or_none()
        if spec:
            snapshot[f"{doc_type}_md"] = spec.content
            found += 1
    if found == 0:
        raise MarketplaceError("Project has no saved spec documents to import")
    sample.spec_snapshot = snapshot
    await db.commit()
    await db.refresh(sample)
    return snapshot


def _publish_problems(sample: MarketplaceSample) -> list[str]:
    problems = []
    snapshot = sample.spec_snapshot or {}
    for doc_type in DOC_TYPES:
        if not (snapshot.get(f"{doc_type}_md") or "").strip():
            problems.append(f"spec_snapshot.{doc_type}_md is empty")
    if not sample.title.strip():
        problems.append("title required")
    if not sample.description.strip():
        problems.append("description required")
    if sample.category not in VALID_CATEGORIES:
        problems.append("valid category required")
    if sample.complexity not in ("beginner", "intermediate", "advanced"):
        problems.append("valid complexity required")
    return problems


async def publish_sample(db: AsyncSession, sample: MarketplaceSample) -> MarketplaceSample:
    if sample.status == "published":
        return sample
    problems = _publish_problems(sample)
    if problems:
        raise MarketplaceError("Cannot publish: " + "; ".join(problems))
    sample.status = "published"
    sample.published_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(sample)
    # Interest-based fan-out (notifications spec producers table): users with a
    # project in this category, capped, best-effort.
    from app.services import notifications as notif

    async def fan_out() -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as session:
            rows = await session.execute(
                select(Project.user_id)
                .join(Template, Project.template_id == Template.id, isouter=True)
                .where(Template.category == sample.category)
                .distinct()
                .limit(50)
            )
            user_ids = [r[0] for r in rows.all()]
            await notif.notify_many(
                session, user_ids,
                type="sample_published",
                title="New marketplace sample",
                body=f'"{sample.title}" was published in a category you build in.',
                link=f"/marketplace/{sample.id}",
                dedupe_key=f"sample_published:{sample.id}",
            )

    notif.emit(fan_out())
    return sample


# ---------------------------------------------------------------- submissions
# (marketplace-submissions spec) — a submission IS a sample row: status
# 'submitted' with a snapshot frozen at submit time. Approval transitions
# into the existing 'draft' curation path; 'rejected'/'withdrawn' are terminal.

SUBMISSION_STATUSES = ("submitted", "rejected", "withdrawn")


async def submit_project(
    db: AsyncSession, project: Project, actor: User, *, title: str,
    summary: str, category: str, keywords: list[str],
) -> MarketplaceSample:
    """Snapshot the project's latest docs into a submitted sample (R1)."""
    if category not in VALID_CATEGORIES:
        raise MarketplaceError(
            f"Unknown category '{category}'. Valid: {', '.join(sorted(VALID_CATEGORIES))}"
        )
    open_row = (
        await db.execute(
            select(MarketplaceSample).where(
                MarketplaceSample.source_project_id == project.id,
                MarketplaceSample.status == "submitted",
            )
        )
    ).scalar_one_or_none()
    if open_row is not None:
        raise SubmissionConflict("This project already has a submission under review")

    snapshot: dict = {}
    for doc_type in DOC_TYPES:
        result = await db.execute(
            select(Spec)
            .where(Spec.project_id == project.id, Spec.type == doc_type)
            .order_by(Spec.version.desc())
            .limit(1)
        )
        spec = result.scalar_one_or_none()
        if spec:
            snapshot[f"{doc_type}_md"] = spec.content
    if not (snapshot.get("requirements_md") or "").strip():
        raise MarketplaceError("Save a requirements document before submitting")

    # Resubmission lineage: link the latest rejected/withdrawn submission (R1.4)
    prior = (
        await db.execute(
            select(MarketplaceSample)
            .where(
                MarketplaceSample.source_project_id == project.id,
                MarketplaceSample.status.in_(("rejected", "withdrawn")),
            )
            .order_by(MarketplaceSample.submitted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sample = MarketplaceSample(
        title=title[:60],
        description=summary[:500],
        category=category,
        keywords=keywords[:10],
        template_id=project.template_id,
        spec_snapshot=snapshot,
        author_id=actor.id,
        status="submitted",
        source_project_id=project.id,
        submitted_at=datetime.now(UTC),
        resubmission_of=prior.id if prior else None,
    )
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    return sample


async def withdraw_submission(
    db: AsyncSession, sample: MarketplaceSample, actor: User
) -> MarketplaceSample:
    if sample.author_id != actor.id:
        raise MarketplaceError("Only the submitter may withdraw")
    if sample.status != "submitted":
        raise MarketplaceError("Only pending submissions can be withdrawn")
    sample.status = "withdrawn"
    await db.commit()
    await db.refresh(sample)
    return sample


async def approve_submission(
    db: AsyncSession, sample: MarketplaceSample, admin: User
) -> MarketplaceSample:
    """submitted → draft: enters the EXISTING curation path (R2.3)."""
    if sample.status != "submitted":
        raise MarketplaceError("Only pending submissions can be approved")
    sample.status = "draft"
    sample.reviewed_by = admin.id
    sample.reviewed_at = datetime.now(UTC)
    sample.review_feedback = None
    await db.commit()
    await db.refresh(sample)
    _notify_author(sample, approved=True)
    return sample


async def reject_submission(
    db: AsyncSession, sample: MarketplaceSample, admin: User, feedback: str
) -> MarketplaceSample:
    if sample.status != "submitted":
        raise MarketplaceError("Only pending submissions can be rejected")
    if not feedback.strip():
        raise MarketplaceError("Feedback is required to reject a submission")
    sample.status = "rejected"
    sample.reviewed_by = admin.id
    sample.reviewed_at = datetime.now(UTC)
    sample.review_feedback = feedback.strip()
    await db.commit()
    await db.refresh(sample)
    _notify_author(sample, approved=False)
    return sample


def _notify_author(sample: MarketplaceSample, *, approved: bool) -> None:
    from app.services import notifications as notif

    if sample.author_id is None:
        return
    author_id = sample.author_id
    if approved:
        title = "Marketplace submission approved"
        body = (
            f'"{sample.title}" was approved — an admin will polish and publish it. '
            "You'll be credited as the contributor."
        )
    else:
        title = "Marketplace submission needs changes"
        body = f'"{sample.title}" was not accepted: {sample.review_feedback}'
    notif.emit(
        notif.emit_for_user(
            author_id,
            type="submission_decided",
            title=title,
            body=body,
            link="/profile",
        )
    )


def notify_admins_of_submission(sample: MarketplaceSample) -> None:
    """Fan-out to platform admins on submit (R1.6)."""
    from app.services import notifications as notif

    sample_title = sample.title
    sample_id = sample.id

    async def fan_out() -> None:
        from app.core.db import SessionLocal

        async with SessionLocal() as session:
            rows = await session.execute(select(User.id).where(User.role == "admin"))
            await notif.notify_many(
                session,
                [r[0] for r in rows.all()],
                type="submission_received",
                title="New marketplace submission",
                body=f'"{sample_title}" is waiting for review.',
                link="/admin/marketplace?tab=submissions",
                dedupe_key=f"submission_received:{sample_id}",
            )

    notif.emit(fan_out())


class SubmissionConflict(Exception):
    """One open submission per project (R1.4) — surfaces as 409."""


async def list_submissions(
    db: AsyncSession, *, status: str | None = "submitted", author_id: uuid.UUID | None = None
) -> list[MarketplaceSample]:
    stmt = select(MarketplaceSample).where(
        MarketplaceSample.source_project_id.is_not(None)
    )
    if author_id is not None:
        stmt = stmt.where(MarketplaceSample.author_id == author_id)
        # Authors see their full submission history (any status incl. published)
    elif status and status != "all":
        stmt = stmt.where(MarketplaceSample.status == status)
    else:
        stmt = stmt.where(
            MarketplaceSample.status.in_(SUBMISSION_STATUSES + ("draft", "published"))
        )
    stmt = stmt.order_by(MarketplaceSample.submitted_at.desc())
    return list((await db.execute(stmt)).scalars())


async def archive_sample(db: AsyncSession, sample: MarketplaceSample) -> MarketplaceSample:
    sample.status = "archived"  # existing forks unaffected (§4.3.6)
    await db.commit()
    await db.refresh(sample)
    return sample


async def delete_sample(db: AsyncSession, sample: MarketplaceSample) -> None:
    if sample.status != "draft":
        raise MarketplaceError("Only draft samples can be deleted; archive published ones")
    await db.delete(sample)
    await db.commit()
