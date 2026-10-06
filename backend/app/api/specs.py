"""Project-scoped spec editor endpoints (S2-02/03) — spec-editor-versioning R3."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import project_editor, project_viewer
from app.core.auth import require_editor_persona
from app.core.db import get_db
from app.models import Project, User
from app.schemas.chat import DocSpecState, FullSpecResponse, SpecOut, SpecVersionInfo
from app.schemas.specs import DraftIn, DraftOut, RollbackIn, SpecSaveIn, SpecSaveOut
from app.services import analytics
from app.services import specs as specs_service
from app.services.spec_validation import DOC_TYPES

router = APIRouter(prefix="/projects/{project_id}/specs", tags=["specs"])


def _check_type(doc_type: str) -> str:
    if doc_type not in DOC_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown document type: {doc_type}")
    return doc_type


@router.get("", response_model=FullSpecResponse)
async def get_specs(
    project: Project = Depends(project_viewer), db: AsyncSession = Depends(get_db)
) -> FullSpecResponse:
    from sqlalchemy import select

    # Batch author names for version attribution (collaboration R3.1)
    names: dict = {}

    async def state(doc_type: str) -> DocSpecState:
        versions = await specs_service.list_versions(db, project.id, doc_type)
        if not versions:
            return DocSpecState()
        author_ids = {v.created_by for v in versions if v.created_by} - names.keys()
        if author_ids:
            rows = await db.execute(select(User).where(User.id.in_(author_ids)))
            names.update({u.id: (u.name or u.email.split("@")[0]) for u in rows.scalars()})
        infos = []
        for v in versions:
            info = SpecVersionInfo.model_validate(v)
            info.created_by_name = names.get(v.created_by) if v.created_by else None
            infos.append(info)
        return DocSpecState(latest=SpecOut.model_validate(versions[0]), versions=infos)

    return FullSpecResponse(
        requirements=await state("requirements"),
        design=await state("design"),
        tasks=await state("tasks"),
    )


@router.get("/{doc_type}/versions/{version}", response_model=SpecOut)
async def get_spec_version(
    doc_type: str,
    version: int,
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> SpecOut:
    _check_type(doc_type)
    spec = await specs_service.get_version(db, project.id, doc_type, version)
    if spec is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return SpecOut.model_validate(spec)


@router.put("/{doc_type}", response_model=SpecSaveOut)
async def save_spec(
    doc_type: str,
    payload: SpecSaveIn,
    project: Project = Depends(project_editor),
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> SpecSaveOut:
    _check_type(doc_type)
    from app.services.capabilities import CapabilityNotAllowed
    from app.services.composition import CompositionError

    try:
        spec, warnings = await specs_service.save_version(
            db, project, doc_type, payload.content, user
        )
    except (CompositionError, CapabilityNotAllowed) as exc:
        # Composable agents R1.3 / agent-substance R3.2: named refusal, save aborted
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    analytics.track(user.id, "spec_saved", project_id=project.id)  # S16-04
    return SpecSaveOut(spec=SpecOut.model_validate(spec), warnings=warnings)


@router.post("/{doc_type}/rollback", response_model=SpecOut)
async def rollback_spec(
    doc_type: str,
    payload: RollbackIn,
    project: Project = Depends(project_editor),
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> SpecOut:
    _check_type(doc_type)
    from app.services.capabilities import CapabilityNotAllowed
    from app.services.composition import CompositionError

    try:
        spec = await specs_service.rollback(db, project, doc_type, payload.version, user)
    except (CompositionError, CapabilityNotAllowed) as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "detail": exc.detail}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SpecOut.model_validate(spec)


@router.get("/{doc_type}/draft", response_model=DraftOut | None)
async def get_draft(
    doc_type: str,
    project: Project = Depends(project_editor),
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> DraftOut | None:
    _check_type(doc_type)
    draft = await specs_service.get_draft(db, project.id, doc_type, user.id)
    return DraftOut.model_validate(draft) if draft else None


@router.put("/{doc_type}/draft", response_model=DraftOut)
async def save_draft(
    doc_type: str,
    payload: DraftIn,
    project: Project = Depends(project_editor),
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> DraftOut:
    _check_type(doc_type)
    draft = await specs_service.upsert_draft(db, project.id, doc_type, user.id, payload.content)
    return DraftOut.model_validate(draft)


@router.delete("/{doc_type}/draft", status_code=204)
async def discard_draft(
    doc_type: str,
    project: Project = Depends(project_editor),
    user: User = Depends(require_editor_persona()),
    db: AsyncSession = Depends(get_db),
) -> None:
    _check_type(doc_type)
    await specs_service.delete_draft(db, project.id, doc_type, user.id)
