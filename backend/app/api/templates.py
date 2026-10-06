"""Template endpoints: admin CRUD/lifecycle + user-facing catalog (S2-04/05)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.models import Template, User
from app.schemas.templates import (
    ModelRegistryEntry,
    TemplateAdminDetail,
    TemplateAdminOut,
    TemplateCreate,
    TemplateUpdate,
    TemplateUserOut,
)
from app.services import templates as template_service

admin_router = APIRouter(
    prefix="/admin/templates",
    tags=["admin-templates"],
    dependencies=[Depends(require_role("admin"))],
)
router = APIRouter(prefix="/templates", tags=["templates"])


async def _admin_template(
    template_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Template:
    template = await template_service.get_template(db, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


# ------------------------------------------------------------------ admin


@admin_router.get("/model-registry", response_model=list[ModelRegistryEntry])
async def model_registry() -> list[ModelRegistryEntry]:
    # S17: merged view — Bedrock built-ins + enabled custom endpoints, so every
    # admin picker (allowlist, template rails, sample import) sees externals.
    from app.services.guardrails import model_registry as merged_registry

    return [ModelRegistryEntry(**m) for m in await merged_registry()]


@admin_router.post("", response_model=TemplateAdminOut, status_code=201)
async def create_template(
    payload: TemplateCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateAdminOut:
    try:
        template = await template_service.create_template(db, user, payload.model_dump())
    except template_service.TemplateLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TemplateAdminOut.model_validate(template)


@admin_router.get("", response_model=list[TemplateAdminOut])
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[TemplateAdminOut]:
    templates = await template_service.list_templates(db)
    return [TemplateAdminOut.model_validate(t) for t in templates]


@admin_router.get("/{template_id}", response_model=TemplateAdminDetail)
async def get_template(
    template: Template = Depends(_admin_template), db: AsyncSession = Depends(get_db)
) -> TemplateAdminDetail:
    detail = TemplateAdminDetail.model_validate(template)
    detail.active_project_count = await template_service.active_project_count(db, template.id)
    return detail


@admin_router.put("/{template_id}", response_model=TemplateAdminOut)
async def update_template(
    payload: TemplateUpdate,
    template: Template = Depends(_admin_template),
    db: AsyncSession = Depends(get_db),
) -> TemplateAdminOut:
    try:
        updated = await template_service.update_template(
            db, template, payload.model_dump(exclude_unset=True)
        )
    except template_service.TemplateLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TemplateAdminOut.model_validate(updated)


@admin_router.post("/{template_id}/publish", response_model=TemplateAdminOut)
async def publish_template(
    template: Template = Depends(_admin_template), db: AsyncSession = Depends(get_db)
) -> TemplateAdminOut:
    try:
        return TemplateAdminOut.model_validate(await template_service.publish(db, template))
    except template_service.TemplateLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@admin_router.post("/{template_id}/deprecate", response_model=TemplateAdminDetail)
async def deprecate_template(
    template: Template = Depends(_admin_template), db: AsyncSession = Depends(get_db)
) -> TemplateAdminDetail:
    try:
        deprecated = await template_service.deprecate(db, template)
    except template_service.TemplateLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    detail = TemplateAdminDetail.model_validate(deprecated)
    detail.active_project_count = await template_service.active_project_count(db, template.id)
    return detail


@admin_router.delete("/{template_id}", status_code=204)
async def delete_template(
    template: Template = Depends(_admin_template), db: AsyncSession = Depends(get_db)
) -> None:
    try:
        await template_service.delete_template(db, template)
    except template_service.TemplateLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ------------------------------------------------------------------ user-facing


def _to_user_out(template: Template, registry: list[dict]) -> TemplateUserOut:
    allowed = ((template.guardrails or {}).get("model") or {}).get("allowed_models") or []
    labels_by_id = {entry["id"]: entry["label"] for entry in registry}
    labels = []
    for model_id in allowed:
        label = labels_by_id.get(model_id)
        if label:
            labels.append(label)
        elif model_id.startswith("ext/"):
            labels.append(f"{model_id.removeprefix('ext/')} (external)")
        else:
            labels.append(model_id)
    return TemplateUserOut(
        id=template.id,
        name=template.name,
        version=template.version,
        description=template.description,
        category=template.category,
        starter_prompts=(template.scaffolding or {}).get("starter_prompts") or [],
        allowed_model_labels=labels,
    )


@router.get("", response_model=list[TemplateUserOut])
async def list_active_templates(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[TemplateUserOut]:
    templates = await template_service.list_templates(db, status="active")
    from app.services.guardrails import model_registry as merged_registry

    registry = await merged_registry()
    return [_to_user_out(template, registry) for template in templates]


@router.get("/{template_id}", response_model=TemplateUserOut)
async def get_active_template(
    template_id: uuid.UUID,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TemplateUserOut:
    template = await template_service.get_template(db, template_id)
    if template is None or template.status != "active":
        raise HTTPException(status_code=404, detail="Template not found")
    from app.services.guardrails import model_registry as merged_registry

    return _to_user_out(template, await merged_registry())
