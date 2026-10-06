"""Shared test fixtures: file-backed SQLite (WAL) + auth override.

File-backed + NullPool gives every session its OWN connection — background
fire-and-forget tasks (audit, notifications, build pipelines) no longer share
a transaction with the test session. The old in-memory StaticPool setup
entangled concurrent sessions on one connection: a rollback in a notify task
could silently discard another session's uncommitted rows (observed as a
~1-in-5 flake in the codegen pipeline tests).
"""

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import Depends, Request

# Fork-safety drill (`AWS_PROFILE= AWS_ACCESS_KEY_ID= … pytest`): botocore reads
# an EMPTY AWS_PROFILE as a profile literally named "" and raises
# ProfileNotFound from every boto3 client/Session constructor — a config
# artifact, not a credential check. Normalize to unset so the no-credentials
# run exercises the real NoCredentials paths. (Empty AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY are already treated as absent by botocore.)
if os.environ.get("AWS_PROFILE") == "":
    del os.environ["AWS_PROFILE"]
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.auth import get_current_user
from app.core.db import get_db
from app.models import Base, User


@pytest_asyncio.fixture(autouse=True)
def _reset_shared_state():
    """Fresh in-process shared-state backend per test (S12): platform-scoped
    counter keys (spend:{month}:platform, rate windows) would otherwise leak
    between tests through the module singleton."""
    from app.services.shared_state import STATE

    STATE.reset_for_tests()
    yield
    STATE.reset_for_tests()


@pytest_asyncio.fixture
async def db_engine(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/test.db",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):  # WAL: concurrent readers + queued writers
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncSession:
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def test_user(db_session) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email="power@marshal.demo",
        name="Power Demo",
        role="power",
        persona="power",
        onboarding_completed=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def client(db_engine, db_session, test_user):
    """HTTPX client against the app with DB + auth overridden.

    The auth override re-fetches the user WITHIN the request's DB session so
    ORM mutations in route handlers work (avoids cross-session detached rows).
    """
    from app.main import create_app
    from app.models import User

    app = create_app()
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            yield session

    async def override_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
        user = await db.get(User, test_user.id)
        request.state.user = user
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def admin_user(db_session) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email="admin@marshal.demo",
        name="Admin Demo",
        role="admin",
        persona="power",
        onboarding_completed=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def business_user(db_session) -> User:
    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}",
        email="business@marshal.demo",
        name="Business Demo",
        role="business",
        persona="business",
        onboarding_completed=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def audit_db(db_engine, monkeypatch):
    """Point fire-and-forget audit/invocation writers at the test database."""
    import app.services.audit as audit_module
    import app.services.bedrock as bedrock_module

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(audit_module, "SessionLocal", maker)
    monkeypatch.setattr("app.core.db.SessionLocal", maker)
    # deployment.py binds SessionLocal at import time (S1-era style) — patch
    # its module binding too so background pipelines hit the test DB.
    import app.services.deployment as deployment_module

    monkeypatch.setattr(deployment_module, "SessionLocal", maker)

    # bedrock._record_invocation imports SessionLocal lazily from app.core.db —
    # the core patch above covers it; return the maker for direct assertions.
    _ = bedrock_module
    return maker


@pytest.fixture(autouse=True)
def _stub_conformance_review(monkeypatch):
    """Hermetic default: the conformance MODEL review never leaves the process
    (codegen-quality R1.4 — the orchestrator degrades to deterministic-only).
    Tests exercising the review path re-patch `review_criteria` themselves."""

    async def _unavailable(*_args, **_kwargs):
        raise RuntimeError("conformance model review is stubbed in tests")

    monkeypatch.setattr(
        "app.services.codegen.conformance.review_criteria", _unavailable
    )


@pytest_asyncio.fixture
async def client_for(db_engine, audit_db):
    """Factory: client_for(user) → AsyncClient authenticated as that user."""
    from contextlib import asynccontextmanager

    from app.main import create_app
    from app.models import User as UserModel

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _build(user):
        app = create_app()

        async def override_get_db():
            async with maker() as session:
                yield session

        async def override_user(request: Request, db: AsyncSession = Depends(get_db)) -> UserModel:
            row = await db.get(UserModel, user.id)
            request.state.user = row
            return row

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_user
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    return _build
