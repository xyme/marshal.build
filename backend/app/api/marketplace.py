"""Marketplace APIs (marketplace spec R6): public catalog + admin curation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import project_owner
from app.core.auth import (
    get_current_user,
    require_admin_security_if_admin,
    require_role,
)
from app.core.db import get_db
from app.models import MarketplaceSample, Project, User
from app.schemas.marketplace import (
    CategoryCountOut,
    ForkIn,
    ForkOut,
    RejectIn,
    SampleAdminOut,
    SampleCardOut,
    SampleDetailOut,
    SampleImportIn,
    SampleListOut,
    SampleUpsertIn,
    SubmissionOut,
    SubmitIn,
)
from app.services import audit
from app.services import marketplace as svc

router = APIRouter(prefix="/marketplace", tags=["marketplace"])
admin_router = APIRouter(
    prefix="/admin/marketplace/samples",
    tags=["admin-marketplace"],
    dependencies=[Depends(require_role("admin"))],
)
# Project-scoped submit + admin review queue (marketplace-submissions spec)
submissions_router = APIRouter(tags=["marketplace-submissions"])
admin_submissions_router = APIRouter(
    prefix="/admin/marketplace/submissions",
    tags=["admin-marketplace"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("/samples", response_model=SampleListOut)
async def list_samples(
    q: str | None = None,
    category: str | None = None,
    complexity: str | None = Query(None, pattern="^(beginner|intermediate|advanced)$"),
    model: str | None = None,
    sort: str = Query("popular", pattern="^(popular|newest|viewed)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=48),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SampleListOut:
    items, total = await svc.list_samples(
        db, q=q, category=category, complexity=complexity, model=model,
        sort=sort, page=page, page_size=page_size,
    )
    # Contributor credit for submission-originated samples (one batched query)
    author_ids = {s.author_id for s in items if s.source_project_id and s.author_id}
    contributors: dict[uuid.UUID, str] = {}
    if author_ids:
        from sqlalchemy import select

        rows = await db.execute(select(User).where(User.id.in_(author_ids)))
        contributors = {u.id: (u.name or u.email.split("@")[0]) for u in rows.scalars()}
    cards = []
    for s in items:
        card = SampleCardOut.model_validate(s)
        card.status = None  # status is admin-only surface area
        if s.source_project_id and s.author_id:
            card.contributed_by = contributors.get(s.author_id)
        cards.append(card)
    return SampleListOut(items=cards, total=total, page=page, page_size=page_size)


@router.get("/categories", response_model=list[CategoryCountOut])
async def categories(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[CategoryCountOut]:
    return [CategoryCountOut(category=c, count=n) for c, n in await svc.category_counts(db)]


@router.get("/samples/{sample_id}", response_model=SampleDetailOut)
async def get_sample(
    sample_id: uuid.UUID,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> SampleDetailOut:
    sample = await svc.get_sample(
        db, sample_id, admin=user.role == "admin", count_view=True
    )
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    out = SampleDetailOut.model_validate(sample)
    for key, value in (await svc.sample_context(db, sample)).items():
        setattr(out, key, value)
    if sample.source_project_id:
        out.contributed_by = out.author_name  # submissions credit (R2.6)
    return out


@router.post("/samples/{sample_id}/fork", response_model=ForkOut, status_code=201)
async def fork_sample(
    sample_id: uuid.UUID,
    payload: ForkIn,
    request: Request,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> ForkOut:
    sample = await svc.get_sample(db, sample_id, admin=user.role == "admin")
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    project, warnings = await svc.fork_sample(db, sample, user, payload.name)
    audit.set_audit_detail(
        request, project_id=str(project.id), sample_title=sample.title, warnings=warnings
    )
    return ForkOut(project_id=project.id, warnings=warnings)


# ------------------------------------------------------------------ submissions


async def _submission_out(db: AsyncSession, sample: MarketplaceSample) -> SubmissionOut:
    out = SubmissionOut.model_validate(sample)
    if sample.author_id:
        author = await db.get(User, sample.author_id)
        if author:
            out.author_name = author.name or author.email.split("@")[0]
            out.author_email = author.email
    if sample.source_project_id:
        project = await db.get(Project, sample.source_project_id)
        if project:
            out.project_name = project.name
    return out


@submissions_router.post(
    "/projects/{project_id}/submit-to-marketplace",
    response_model=SubmissionOut,
    status_code=201,
)
async def submit_to_marketplace(
    payload: SubmitIn,
    request: Request,
    project: Project = Depends(project_owner),
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    """US-018: power users share a project as a marketplace sample."""
    if user.role != "admin" and user.persona != "power":
        raise HTTPException(
            status_code=403,
            detail="Marketplace submissions are part of the Power User experience.",
        )
    try:
        sample = await svc.submit_project(
            db, project, user,
            title=payload.title, summary=payload.summary,
            category=payload.category, keywords=payload.keywords,
        )
    except svc.SubmissionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, resource_id=str(sample.id), title=sample.title)
    svc.notify_admins_of_submission(sample)
    return await _submission_out(db, sample)


@router.get("/my-submissions", response_model=list[SubmissionOut])
async def my_submissions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SubmissionOut]:
    rows = await svc.list_submissions(db, author_id=user.id)
    return [await _submission_out(db, s) for s in rows]


@router.post("/submissions/{sample_id}/withdraw", response_model=SubmissionOut)
async def withdraw_submission(
    sample_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    sample = await db.get(MarketplaceSample, sample_id)
    if sample is None or sample.author_id != user.id:
        raise HTTPException(status_code=404, detail="Submission not found")
    try:
        sample = await svc.withdraw_submission(db, sample, user)
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, title=sample.title)
    return await _submission_out(db, sample)


@admin_submissions_router.get("", response_model=list[SubmissionOut])
async def admin_list_submissions(
    status: str = Query("submitted", pattern="^(submitted|rejected|withdrawn|all)$"),
    db: AsyncSession = Depends(get_db),
) -> list[SubmissionOut]:
    rows = await svc.list_submissions(db, status=status)
    return [await _submission_out(db, s) for s in rows]


@admin_submissions_router.post("/{sample_id}/approve", response_model=SubmissionOut)
async def admin_approve_submission(
    sample_id: uuid.UUID,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    sample = await db.get(MarketplaceSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    try:
        sample = await svc.approve_submission(db, sample, admin)
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, title=sample.title, author=str(sample.author_id))
    return await _submission_out(db, sample)


@admin_submissions_router.post("/{sample_id}/reject", response_model=SubmissionOut)
async def admin_reject_submission(
    sample_id: uuid.UUID,
    payload: RejectIn,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> SubmissionOut:
    sample = await db.get(MarketplaceSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    try:
        sample = await svc.reject_submission(db, sample, admin, payload.feedback)
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, title=sample.title, feedback=payload.feedback)
    return await _submission_out(db, sample)


# ------------------------------------------------------------------ admin


async def _admin_sample(
    sample_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> MarketplaceSample:
    sample = await db.get(MarketplaceSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample


def _admin_out(sample: MarketplaceSample) -> SampleAdminOut:
    return SampleAdminOut.model_validate(sample)


@admin_router.get("", response_model=SampleListOut)
async def admin_list(
    q: str | None = None,
    status: str | None = Query(
        None, pattern="^(draft|submitted|rejected|withdrawn|published|archived)$"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> SampleListOut:
    items, total = await svc.list_samples(
        db, q=q, page=page, page_size=page_size, sort="newest",
        include_all_statuses=True, status=status,
    )
    return SampleListOut(
        items=[SampleCardOut.model_validate(s) for s in items],
        total=total, page=page, page_size=page_size,
    )


@admin_router.get("/{sample_id}", response_model=SampleAdminOut)
async def admin_get(sample: MarketplaceSample = Depends(_admin_sample)) -> SampleAdminOut:
    return _admin_out(sample)


@admin_router.post("", response_model=SampleAdminOut, status_code=201)
async def admin_create(
    payload: SampleUpsertIn,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> SampleAdminOut:
    try:
        return _admin_out(await svc.create_sample(db, payload, admin))
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@admin_router.put("/{sample_id}", response_model=SampleAdminOut)
async def admin_update(
    payload: SampleUpsertIn,
    sample: MarketplaceSample = Depends(_admin_sample),
    db: AsyncSession = Depends(get_db),
) -> SampleAdminOut:
    try:
        return _admin_out(await svc.update_sample(db, sample, payload))
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@admin_router.post("/{sample_id}/import-spec", response_model=SampleAdminOut)
async def admin_import_spec(
    payload: SampleImportIn,
    sample: MarketplaceSample = Depends(_admin_sample),
    db: AsyncSession = Depends(get_db),
) -> SampleAdminOut:
    try:
        await svc.import_snapshot(db, sample, payload.project_id)
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _admin_out(sample)


@admin_router.post("/{sample_id}/publish", response_model=SampleAdminOut)
async def admin_publish(
    sample: MarketplaceSample = Depends(_admin_sample), db: AsyncSession = Depends(get_db)
) -> SampleAdminOut:
    try:
        return _admin_out(await svc.publish_sample(db, sample))
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@admin_router.post("/{sample_id}/archive", response_model=SampleAdminOut)
async def admin_archive(
    sample: MarketplaceSample = Depends(_admin_sample), db: AsyncSession = Depends(get_db)
) -> SampleAdminOut:
    return _admin_out(await svc.archive_sample(db, sample))


@admin_router.delete("/{sample_id}", status_code=204)
async def admin_delete(
    sample: MarketplaceSample = Depends(_admin_sample), db: AsyncSession = Depends(get_db)
) -> None:
    try:
        await svc.delete_sample(db, sample)
    except svc.MarketplaceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
