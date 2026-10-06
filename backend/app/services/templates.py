"""Template lifecycle service (S2-04) — FSD §4.2.4 state machine subset."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Template, User

VALID_CATEGORIES = {
    "chatbot",
    "document_processing",
    "data_analysis",
    "workflow_automation",
    "content_generation",
    "custom",
}


class TemplateLifecycleError(Exception):
    pass


async def list_templates(db: AsyncSession, status: str | None = None) -> list[Template]:
    query = select(Template).order_by(Template.updated_at.desc())
    if status:
        query = query.where(Template.status == status)
    return list((await db.execute(query)).scalars())


async def get_template(db: AsyncSession, template_id: uuid.UUID) -> Template | None:
    return await db.get(Template, template_id)


def _validate_guardrails(guardrails: dict | None) -> None:
    """Blob-shape validation on write (agent-substance R3.2): the capability
    rail must name real capabilities and a sane loop ceiling."""
    from app.services.capabilities import validate_capabilities_blob

    try:
        validate_capabilities_blob(guardrails)
    except ValueError as exc:
        raise TemplateLifecycleError(str(exc)) from exc


async def create_template(db: AsyncSession, user: User, data: dict) -> Template:
    _validate_guardrails(data.get("guardrails"))
    template = Template(
        name=data["name"],
        description=data.get("description"),
        category=data.get("category", "custom"),
        guardrails=data.get("guardrails") or {},
        scaffolding=data.get("scaffolding") or {},
        status="draft",
        created_by=user.id,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return template


async def update_template(db: AsyncSession, template: Template, data: dict) -> Template:
    if data.get("guardrails") is not None:
        _validate_guardrails(data["guardrails"])
    guardrails_changed = (
        "guardrails" in data and data["guardrails"] != template.guardrails
    )
    for field in ("name", "description", "category"):
        if field in data and data[field] is not None:
            setattr(template, field, data[field])
    if "guardrails" in data and data["guardrails"] is not None:
        template.guardrails = data["guardrails"]
    if "scaffolding" in data and data["scaffolding"] is not None:
        template.scaffolding = data["scaffolding"]
    # Editing an ACTIVE template's guardrails bumps the version (R1.2)
    if template.status == "active" and guardrails_changed:
        template.version += 1
    await db.commit()
    await db.refresh(template)
    return template


def _validate_publishable(template: Template) -> None:
    model_rails = (template.guardrails or {}).get("model") or {}
    allowed = model_rails.get("allowed_models") or []
    if not allowed:
        raise TemplateLifecycleError(
            "At least one guardrail category must be configured before publishing "
            "(model guardrails with one or more allowed models)."
        )


async def publish(db: AsyncSession, template: Template) -> Template:
    if template.status == "deprecated":
        raise TemplateLifecycleError("Deprecated templates cannot be republished")
    _validate_publishable(template)
    template.status = "active"
    await db.commit()
    await db.refresh(template)
    return template


async def deprecate(db: AsyncSession, template: Template) -> Template:
    if template.status != "active":
        raise TemplateLifecycleError("Only active templates can be deprecated")
    template.status = "deprecated"
    await db.commit()
    await db.refresh(template)
    return template


async def delete_template(db: AsyncSession, template: Template) -> None:
    if template.status != "draft":
        raise TemplateLifecycleError("Only draft templates can be deleted")
    await db.delete(template)
    await db.commit()


async def active_project_count(db: AsyncSession, template_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.template_id == template_id)
        )
        or 0
    )
