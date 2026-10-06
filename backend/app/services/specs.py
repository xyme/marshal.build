"""Spec editing service: versions, rollback, autosave drafts (S2-02/03)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Spec, SpecDraft, User
from app.services.spec_validation import validate_doc


async def next_version(db: AsyncSession, project_id: uuid.UUID, doc_type: str) -> int:
    current = await db.scalar(
        select(func.max(Spec.version)).where(
            Spec.project_id == project_id, Spec.type == doc_type
        )
    )
    return (current or 0) + 1


async def list_versions(db: AsyncSession, project_id: uuid.UUID, doc_type: str) -> list[Spec]:
    result = await db.execute(
        select(Spec)
        .where(Spec.project_id == project_id, Spec.type == doc_type)
        .order_by(Spec.version.desc())
    )
    return list(result.scalars())


async def get_version(
    db: AsyncSession, project_id: uuid.UUID, doc_type: str, version: int
) -> Spec | None:
    result = await db.execute(
        select(Spec).where(
            Spec.project_id == project_id, Spec.type == doc_type, Spec.version == version
        )
    )
    return result.scalar_one_or_none()


async def save_version(
    db: AsyncSession,
    project: Project,
    doc_type: str,
    content: str,
    user: User,
    origin: str = "edited",
) -> tuple[Spec, list[str]]:
    """Create the next version; returns (spec, advisory validation warnings)."""
    warnings = validate_doc(doc_type, content)
    if doc_type == "requirements":
        # Composable agents R1.2: the stored graph tracks the LATEST content —
        # refusals (cycle/depth/unresolved) abort the save with a named 422.
        from app.services.composition import sync_project_composition

        await sync_project_composition(db, project, content)
        # Agent substance R3.2: the template capability rail refuses forbidden
        # rung declarations at the same seam (AC-4 — never a deploy surprise).
        from app.services.capabilities import enforce_template_rail

        await enforce_template_rail(db, project, content)
    spec = Spec(
        project_id=project.id,
        version=await next_version(db, project.id, doc_type),
        type=doc_type,
        content=content,
        origin=origin,
        created_by=user.id,
    )
    db.add(spec)
    # Manual save supersedes the author's draft
    await delete_draft(db, project.id, doc_type, user.id, commit=False)
    await db.commit()
    await db.refresh(spec)
    # Risk re-scoring on content change (S4-02 R2.1; hash-cached, debounced)
    from app.services.risk import trigger_assessment

    trigger_assessment(project.id, user.id)
    return spec, warnings


async def rollback(
    db: AsyncSession, project: Project, doc_type: str, version: int, user: User
) -> Spec:
    """Non-destructive rollback: copy version N's content as a NEW latest version."""
    source = await get_version(db, project.id, doc_type, version)
    if source is None:
        raise ValueError(f"Version {version} of {doc_type} not found")
    if doc_type == "requirements":
        # Rolling back requirements re-syncs the graph to the restored content
        from app.services.composition import sync_project_composition

        await sync_project_composition(db, project, source.content)
        from app.services.capabilities import enforce_template_rail

        await enforce_template_rail(db, project, source.content)
    spec = Spec(
        project_id=project.id,
        version=await next_version(db, project.id, doc_type),
        type=doc_type,
        content=source.content,
        model_id=source.model_id,
        origin="rollback",
        created_by=user.id,
    )
    db.add(spec)
    await db.commit()
    await db.refresh(spec)
    return spec


async def get_draft(
    db: AsyncSession, project_id: uuid.UUID, doc_type: str, user_id: uuid.UUID
) -> SpecDraft | None:
    result = await db.execute(
        select(SpecDraft).where(
            SpecDraft.project_id == project_id,
            SpecDraft.type == doc_type,
            SpecDraft.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_draft(
    db: AsyncSession, project_id: uuid.UUID, doc_type: str, user_id: uuid.UUID, content: str
) -> SpecDraft:
    draft = await get_draft(db, project_id, doc_type, user_id)
    if draft is None:
        draft = SpecDraft(
            project_id=project_id, type=doc_type, user_id=user_id, content=content
        )
        db.add(draft)
    else:
        draft.content = content
    await db.commit()
    await db.refresh(draft)
    return draft


async def delete_draft(
    db: AsyncSession,
    project_id: uuid.UUID,
    doc_type: str,
    user_id: uuid.UUID,
    commit: bool = True,
) -> None:
    draft = await get_draft(db, project_id, doc_type, user_id)
    if draft is not None:
        await db.delete(draft)
        if commit:
            await db.commit()
