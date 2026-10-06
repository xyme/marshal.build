"""Spec editor service: versions, rollback, drafts (spec-editor spec R1/R2)."""

import pytest

from app.models import Project, Spec
from app.services import specs as specs_service


@pytest.fixture
async def project(db_session, test_user) -> Project:
    proj = Project(user_id=test_user.id, name="Editable")
    db_session.add(proj)
    await db_session.flush()  # materialize proj.id before referencing it
    db_session.add(
        Spec(project_id=proj.id, version=1, type="tasks", content="# Implementation Plan — A\n- [ ] 1. x _(Req: FR-1)_\n  - Test: t")
    )
    await db_session.commit()
    await db_session.refresh(proj)
    return proj


async def test_save_version_increments_and_clears_draft(db_session, test_user, project):
    await specs_service.upsert_draft(db_session, project.id, "tasks", test_user.id, "draft txt")
    spec, warnings = await specs_service.save_version(
        db_session, project, "tasks", "# Implementation Plan — A\n- [ ] 1. y _(Req: FR-1)_\n  - Test: t", test_user
    )
    assert spec.version == 2
    assert spec.origin == "edited"
    assert spec.created_by == test_user.id
    assert warnings == []
    assert await specs_service.get_draft(db_session, project.id, "tasks", test_user.id) is None


async def test_save_invalid_content_saves_with_warnings(db_session, test_user, project):
    spec, warnings = await specs_service.save_version(
        db_session, project, "tasks", "no checkboxes here", test_user
    )
    assert spec.version == 2
    assert warnings  # advisory, not blocking


async def test_rollback_creates_new_version_with_old_content(db_session, test_user, project):
    v1 = await specs_service.get_version(db_session, project.id, "tasks", 1)
    await specs_service.save_version(db_session, project, "tasks", "# Implementation Plan — B\n- [ ] 1. z\n  - Test: t", test_user)
    rolled = await specs_service.rollback(db_session, project, "tasks", 1, test_user)
    assert rolled.version == 3
    assert rolled.origin == "rollback"
    assert rolled.content == v1.content


async def test_rollback_missing_version_raises(db_session, test_user, project):
    with pytest.raises(ValueError, match="not found"):
        await specs_service.rollback(db_session, project, "tasks", 99, test_user)


async def test_draft_upsert_and_discard(db_session, test_user, project):
    d1 = await specs_service.upsert_draft(db_session, project.id, "design", test_user.id, "v1")
    d2 = await specs_service.upsert_draft(db_session, project.id, "design", test_user.id, "v2")
    assert d1.id == d2.id
    assert d2.content == "v2"
    await specs_service.delete_draft(db_session, project.id, "design", test_user.id)
    assert await specs_service.get_draft(db_session, project.id, "design", test_user.id) is None
