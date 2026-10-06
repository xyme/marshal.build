"""Generation job state machine (multi-doc spec R1/R2) — Bedrock stubbed."""

from unittest.mock import patch

import pytest

from app.models import ChatSession, SpecGeneration
from app.services import specgen
from tests.test_spec_validation import VALID_DESIGN, VALID_REQUIREMENTS, VALID_TASKS

DOC_CONTENT = {
    "requirements": VALID_REQUIREMENTS,
    "design": VALID_DESIGN,
    "tasks": VALID_TASKS,
}


@pytest.fixture
async def session(db_session, test_user) -> ChatSession:
    chat = ChatSession(user_id=test_user.id, title="Bot idea")
    db_session.add(chat)
    await db_session.commit()
    await db_session.refresh(chat)
    return chat


@pytest.fixture
def fake_history():
    async def get_messages(session_id):
        return [{"role": "user", "content": "Build me a document Q&A bot", "sk": "1"}]

    with patch.object(specgen.chat_service, "get_messages", side_effect=get_messages):
        yield


@pytest.fixture
def fake_converse():
    """Return valid content per doc type based on the system prompt used."""

    async def converse(*, messages, system, model_id, max_tokens=None, ctx=None):
        if "specification writer" in system:
            return DOC_CONTENT["requirements"], {}, "end_turn"
        if "solution architect" in system:
            return DOC_CONTENT["design"], {}, "end_turn"
        if "delivery planner" in system:
            return DOC_CONTENT["tasks"], {}, "end_turn"
        return "Test Project", {}, "end_turn"  # name suggestion

    with patch.object(specgen, "converse", side_effect=converse):
        yield


async def test_start_generation_requires_messages(db_session, test_user, session):
    async def empty(session_id):
        return []

    with patch.object(specgen.chat_service, "get_messages", side_effect=empty):
        with pytest.raises(ValueError, match="no messages"):
            await specgen.start_generation(db_session, test_user, session)


async def test_single_running_generation_guard(db_session, test_user, session, fake_history):
    db_session.add(SpecGeneration(session_id=session.id, status="running", docs=[]))
    await db_session.commit()
    with pytest.raises(specgen.GenerationInProgress):
        await specgen.start_generation(db_session, test_user, session)


async def test_unknown_doc_type_rejected(db_session, test_user, session, fake_history):
    with pytest.raises(ValueError, match="Unknown document type"):
        await specgen.start_generation(db_session, test_user, session, only_type="wishes")


async def test_full_generation_produces_three_docs(
    db_session, db_engine, test_user, session, fake_history, fake_converse, monkeypatch
):
    # Route the job's own DB session factory at the test engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)

    generation = await specgen.start_generation(db_session, test_user, session)
    assert generation.status == "running"
    assert [d["type"] for d in generation.docs] == ["requirements", "design", "tasks"]

    task = specgen._tasks.get(str(generation.id))
    assert task is not None
    await task  # run pipeline to completion

    await db_session.refresh(generation)
    assert generation.status == "done"
    assert all(d["status"] == "done" for d in generation.docs)
    assert generation.finished_at is not None

    await db_session.refresh(session)
    assert session.status == "preview"
    assert session.project_id is not None

    from app.models import Project

    project = await db_session.get(Project, session.project_id)
    assert project.status == "spec_complete"
    for doc_type in ("requirements", "design", "tasks"):
        latest = await specgen.latest_spec(db_session, project.id, doc_type)
        assert latest is not None and latest.version == 1 and latest.origin == "generated"


async def test_rehydrate_marks_running_failed(db_session, db_engine, session, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)

    generation = SpecGeneration(
        session_id=session.id,
        status="running",
        docs=[{"type": "requirements", "status": "generating"}],
    )
    session.status = "generating"
    db_session.add(generation)
    await db_session.commit()

    await specgen.rehydrate_inflight_generations()

    await db_session.refresh(generation)
    await db_session.refresh(session)
    assert generation.status == "failed"
    assert "retry" in generation.error.lower()
    assert generation.docs[0]["status"] == "failed"
    assert session.status == "chatting"


async def test_failed_requirements_skips_dependents_gracefully(
    db_session, db_engine, test_user, session, fake_history, monkeypatch
):
    """R1.5: a failed upstream doc must SKIP dependents, not crash them."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)

    async def converse(*, messages, system, model_id, max_tokens=None, ctx=None):
        if "specification writer" in system:
            return "truncated garbage", {}, "max_tokens"  # cap hit → hard failure
        return "Test Project", {}, "end_turn"

    with patch.object(specgen, "converse", side_effect=converse):
        generation = await specgen.start_generation(db_session, test_user, session)
        await specgen._tasks[str(generation.id)]

    await db_session.refresh(generation)
    assert generation.status == "failed"
    by_type = {d["type"]: d for d in generation.docs}
    assert "max_tokens cap" in by_type["requirements"]["error"]
    assert by_type["design"]["status"] == "failed"
    assert "Skipped: depends on requirements" in by_type["design"]["error"]
    assert "Skipped: depends on" in by_type["tasks"]["error"]


async def test_generation_grounds_on_substrate(
    db_session, db_engine, test_user, session, fake_history, monkeypatch
):
    """Substrate rides every document's SYSTEM prompt (brownfield spec R3) —
    including tasks, which receives no transcript at all."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session.substrate = "SYSTEM: existing invoice-matching agent, tools: sap, s3"
    session.substrate_source = "legacy invoice bot"
    await db_session.commit()

    seen_systems: dict[str, str] = {}

    async def converse(*, messages, system, model_id, max_tokens=None, ctx=None):
        if "specification writer" in system:
            seen_systems["requirements"] = system
            return DOC_CONTENT["requirements"], {}, "end_turn"
        if "solution architect" in system:
            seen_systems["design"] = system
            return DOC_CONTENT["design"], {}, "end_turn"
        if "delivery planner" in system:
            seen_systems["tasks"] = system
            return DOC_CONTENT["tasks"], {}, "end_turn"
        return "Test Project", {}, "end_turn"

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)

    with patch.object(specgen, "converse", side_effect=converse):
        generation = await specgen.start_generation(db_session, test_user, session)
        await specgen._tasks[str(generation.id)]

    await db_session.refresh(generation)
    assert generation.status == "done"
    assert set(seen_systems) == {"requirements", "design", "tasks"}
    for doc_type, system in seen_systems.items():
        assert "BROWNFIELD MIGRATION CONTEXT" in system, doc_type
        assert "invoice-matching agent" in system, doc_type


# ------------------------------------------ title guard + budget (13.5AB)
# Live 30 Sep 2026: the 30-token project-name call stalled 183 s (Bedrock
# read timeout + retry) and consumed 61% of the 300 s set budget; tasks.md
# was killed 4 s in. The same call answered conversationally and the first
# 80 chars became the project name.


def test_generation_budget_pins():
    assert specgen.GENERATION_TIMEOUT_S == 420
    assert specgen.TITLE_TIMEOUT_S <= 15  # cosmetic call can never eat the budget


def test_valid_project_name_shape():
    ok = specgen.valid_project_name
    assert ok("Loan Approval Workflow") == "Loan Approval Workflow"
    assert ok('"X402 Service Provider"\n') == "X402 Service Provider"
    assert ok("  Document   Compliance Checker ") == "Document Compliance Checker"
    # the live failure: a sentence, not a name
    assert ok(
        "I don't see any attached specs in your message. However, based on your request f"
    ) is None
    assert ok("One two three four five six seven") is None  # > 6 words
    assert ok("Why not?") is None  # sentence punctuation
    assert ok("Line one\nLine two") is None
    assert ok("") is None and ok("   ") is None


def test_fallback_project_name_derivation():
    from app.models import ChatSession

    s = ChatSession(title="Bot idea")
    assert specgen.fallback_project_name(s, "[user]: anything") == "Bot idea"
    s = ChatSession(title="New chat")  # default-ish titles don't count
    transcript = "[user]: Build me an X402 service provider for micropayments!\n\n[assistant]: Sure"
    assert specgen.fallback_project_name(s, transcript) == "Build me an X402 service"
    s = ChatSession(title="New chat")
    assert specgen.fallback_project_name(s, "") == "Untitled project"


async def test_title_stall_uses_fallback_and_never_blocks_generation(
    db_session, db_engine, test_user, session, fake_history, monkeypatch
):
    """A stalled naming call is cut at TITLE_TIMEOUT_S; documents still generate."""
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models import Project

    monkeypatch.setattr(specgen, "TITLE_TIMEOUT_S", 0.2)
    session.title = "New chat"  # the default title — carries no signal
    await db_session.commit()

    async def converse(*, messages, system, model_id, max_tokens=None, ctx=None):
        if "specification writer" in system:
            return DOC_CONTENT["requirements"], {}, "end_turn"
        if "solution architect" in system:
            return DOC_CONTENT["design"], {}, "end_turn"
        if "delivery planner" in system:
            return DOC_CONTENT["tasks"], {}, "end_turn"
        await asyncio.sleep(5)  # the naming call "stalls" far past the cap
        return "Never Arrives", {}, "end_turn"

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)
    with patch.object(specgen, "converse", side_effect=converse):
        started = asyncio.get_running_loop().time()
        generation = await specgen.start_generation(db_session, test_user, session)
        await specgen._tasks[str(generation.id)]
        elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 4, "title stall must not delay the pipeline"
    await db_session.refresh(generation)
    assert generation.status == "done"
    await db_session.refresh(session)
    project = await db_session.get(Project, session.project_id)
    assert project.name == "Build me a document Q&A"  # first 5 words of the opening message


async def test_chatty_title_rejected_for_fallback(
    db_session, db_engine, test_user, session, fake_history, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models import Project

    async def converse(*, messages, system, model_id, max_tokens=None, ctx=None):
        if "specification writer" in system:
            return DOC_CONTENT["requirements"], {}, "end_turn"
        if "solution architect" in system:
            return DOC_CONTENT["design"], {}, "end_turn"
        if "delivery planner" in system:
            return DOC_CONTENT["tasks"], {}, "end_turn"
        return (
            "I don't see any attached specs in your message. However, based on "
            "your request for guidance on building an \"X402 Service Provider,\" here"
        ), {}, "end_turn"

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)
    with patch.object(specgen, "converse", side_effect=converse):
        generation = await specgen.start_generation(db_session, test_user, session)
        await specgen._tasks[str(generation.id)]
    await db_session.refresh(session)
    project = await db_session.get(Project, session.project_id)
    assert project.name == "Bot idea"  # the session title, not the model's sentence


# --------------------------------------- session delete after generation (13.5AB)
# Live 30 Sep 2026: DELETE /chat/sessions/{id} 500'd (ForeignKeyViolation from
# spec_generations.session_id) for any session that ever generated — AFTER the
# transcript had already been wiped, leaving a zombie session.


async def test_delete_session_after_generation_detaches_provenance(
    db_session, db_engine, test_user, session, fake_history, fake_converse, client_for, monkeypatch
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models import ChatSession, Project, Spec
    from app.services import chat as chat_service

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(specgen, "SessionLocal", maker)
    generation = await specgen.start_generation(db_session, test_user, session)
    await specgen._tasks[str(generation.id)]
    await db_session.refresh(session)
    project_id = session.project_id
    assert project_id is not None

    order: list[str] = []

    async def fake_delete_messages(session_id):
        order.append("transcript")

    monkeypatch.setattr(chat_service, "delete_messages", fake_delete_messages)
    session_id, generation_id = session.id, generation.id

    async with client_for(test_user) as client:
        r = await client.delete(f"/api/v1/chat/sessions/{session_id}")
        assert r.status_code == 204, r.text
        # the row is gone …
        r = await client.get(f"/api/v1/chat/sessions/{session_id}")
        assert r.status_code == 404
    assert order == ["transcript"]  # transcript cleanup ran (after the row delete)

    db_session.expire_all()
    assert await db_session.get(ChatSession, session_id) is None
    # … and the project keeps its specs + generation history, detached
    specs = (await db_session.execute(select(Spec).where(Spec.project_id == project_id))).scalars().all()
    assert len(specs) == 3 and all(s.session_id is None for s in specs)
    gen = await db_session.get(SpecGeneration, generation_id)
    assert gen is not None and gen.session_id is None and gen.project_id == project_id
    assert (await db_session.get(Project, project_id)).status == "spec_complete"


async def test_delete_session_row_survives_transcript_failure(
    db_session, test_user, session, client_for, monkeypatch
):
    """Ordering: a DynamoDB failure after the row delete must not 500 nor
    resurrect the row — the opposite of the live zombie."""
    from app.models import ChatSession
    from app.services import chat as chat_service

    async def boom(session_id):
        raise RuntimeError("dynamo unavailable")

    monkeypatch.setattr(chat_service, "delete_messages", boom)
    session_id = session.id
    async with client_for(test_user) as client:
        r = await client.delete(f"/api/v1/chat/sessions/{session_id}")
        assert r.status_code == 204
    db_session.expire_all()
    assert await db_session.get(ChatSession, session_id) is None
