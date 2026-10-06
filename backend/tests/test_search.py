"""Global search (beta-usability spec R2 / FSD B12): nothing searchable that
is not listable — access filtering IS the feature."""

import uuid

import pytest

from app.models import (
    ChatSession,
    MarketplaceSample,
    PlatformSettings,
    Project,
    ProjectMember,
    Spec,
    User,
)
from app.services import platform_settings as settings_svc

pytestmark = pytest.mark.asyncio


async def _other_user(db) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:10]}",
        email=f"other-{uuid.uuid4().hex[:6]}@marshal.demo",
        role="power",
        persona="power",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _seed(db, me: User, other: User) -> None:
    db.add_all(
        [
            Project(user_id=me.id, name="Mortgage Advisor", status="spec_complete"),
            Project(user_id=me.id, name="Mortgage Archive", status="archived"),
            Project(user_id=other.id, name="Mortgage Secret", status="spec_complete"),
            ChatSession(user_id=me.id, title="Mortgage bands discussion"),
            ChatSession(user_id=other.id, title="Mortgage private thread"),
            MarketplaceSample(
                title="Mortgage Pre-Qual Sample", description="FS sample",
                category="workflow_automation", complexity="beginner",
                keywords=["mortgage"], status="published",
            ),
            MarketplaceSample(
                title="Mortgage Draft Sample", description="unpublished",
                category="workflow_automation", complexity="beginner",
                keywords=[], status="draft",
            ),
        ]
    )
    await db.commit()


async def _search(client, q: str) -> dict:
    r = await client.get(f"/api/v1/search?q={q}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_search_filters_by_access(db_session, test_user, client_for):
    other = await _other_user(db_session)
    await _seed(db_session, test_user, other)
    async with client_for(test_user) as client:
        data = await _search(client, "mortgage")
        groups = data["groups"]
        # my live project, not archived, NEVER the other user's
        assert [p["name"] for p in groups["projects"]] == ["Mortgage Advisor"]
        # my session only — the visibility clause is the session list's
        assert [s["title"] for s in groups["sessions"]] == ["Mortgage bands discussion"]
        # published samples only
        assert [s["title"] for s in groups["samples"]] == ["Mortgage Pre-Qual Sample"]
        assert "specs" not in groups  # flag off (R2.3)


async def test_shared_project_becomes_searchable(db_session, test_user, client_for):
    other = await _other_user(db_session)
    theirs = Project(user_id=other.id, name="Mortgage Shared", status="spec_complete")
    db_session.add(theirs)
    await db_session.flush()
    db_session.add(
        ProjectMember(project_id=theirs.id, user_id=test_user.id, role="viewer", added_by=other.id)
    )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _search(client, "mortgage")
        assert [p["name"] for p in data["groups"]["projects"]] == ["Mortgage Shared"]


async def test_short_query_short_circuits(test_user, client_for):
    async with client_for(test_user) as client:
        data = await _search(client, "m")
        assert data["groups"] == {"projects": [], "sessions": [], "samples": []}


async def test_group_cap(db_session, test_user, client_for):
    for i in range(7):
        db_session.add(
            Project(user_id=test_user.id, name=f"Capped {i}", status="draft")
        )
    await db_session.commit()
    async with client_for(test_user) as client:
        data = await _search(client, "capped")
        assert len(data["groups"]["projects"]) == 5


async def test_spec_content_flag(db_session, test_user, client_for):
    project = Project(user_id=test_user.id, name="Flagged", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    db_session.add_all(
        [
            Spec(project_id=project.id, version=1, type="requirements",
                 content="# v1\nprequalification bands likely/possible/refer"),
            Spec(project_id=project.id, version=2, type="requirements",
                 content="# v2\nprequalification bands likely/possible/refer, refined"),
        ]
    )
    row = await db_session.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db_session.add(row)
    row.feature_flags = {**(row.feature_flags or {}), "global_search_spec_content": True}
    await db_session.commit()
    settings_svc.invalidate_cache()
    try:
        async with client_for(test_user) as client:
            data = await _search(client, "prequalification")
            specs = data["groups"]["specs"]
            # deduped to the LATEST version per (project, doc)
            assert len(specs) == 1
            assert specs[0]["version"] == 2 and specs[0]["type"] == "requirements"
            assert specs[0]["project_name"] == "Flagged"
    finally:
        row.feature_flags = {**(row.feature_flags or {}), "global_search_spec_content": False}
        await db_session.commit()
        settings_svc.invalidate_cache()
