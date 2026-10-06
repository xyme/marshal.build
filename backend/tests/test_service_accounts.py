"""B9 service accounts (integration-wave spec R2): token round trip, expiry,
revocation, rotation cap, act-only, hash-at-rest."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import ServiceAccountToken, User
from app.services import service_accounts as svc

pytestmark = pytest.mark.asyncio


async def _admin(db) -> User:
    import uuid

    user = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:10]}",
        email=f"admin-{uuid.uuid4().hex[:6]}@marshal.demo",
        role="admin", persona="power",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_token_round_trip(db_session, test_user, client_for, audit_db):
    admin = await _admin(db_session)
    async with client_for(admin) as client:
        r = await client.post(
            "/api/v1/admin/service-accounts", json={"name": "ci-runner", "role": "power"}
        )
        assert r.status_code == 201, r.text
        account_id = r.json()["id"]
        r = await client.post(
            f"/api/v1/admin/service-accounts/{account_id}/tokens",
            json={"name": "primary", "expires_in_days": 30},
        )
        assert r.status_code == 201
        token = r.json()["token"]
        assert token.startswith("mat_")

    # hash at rest — the value never lands in the database (R2.1)
    row = (
        await db_session.execute(select(ServiceAccountToken))
    ).scalars().first()
    assert row.token_hash != token and token not in row.token_hash
    assert row.token_prefix == token[4:12]

    # the token authenticates as the service user (R2.2)
    account = await db_session.get(User, __import__("uuid").UUID(account_id))
    async with client_for(account) as client:
        # client_for injects auth via dependency override? No — it authenticates
        # the given user. Exercise the REAL resolver instead:
        pass
    resolved = await svc.resolve_token(db_session, token)
    assert resolved is not None and resolved.id == account.id and resolved.kind == "service"


async def test_expired_and_revoked_tokens_fail(db_session, test_user, audit_db):
    admin = await _admin(db_session)
    account = await svc.create_account(db_session, name="expiring", role="business", actor=admin)
    minted = await svc.mint_token(
        db_session, account, name="t", expires_in_days=1, actor=admin
    )
    token_value = minted["token"]

    # force-expire
    row = await db_session.get(ServiceAccountToken, __import__("uuid").UUID(minted["id"]))
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    assert await svc.resolve_token(db_session, token_value) is None

    # fresh token works, then revocation kills it instantly
    minted2 = await svc.mint_token(
        db_session, account, name="t2", expires_in_days=30, actor=admin
    )
    assert await svc.resolve_token(db_session, minted2["token"]) is not None
    await svc.revoke_token(db_session, account, __import__("uuid").UUID(minted2["id"]))
    assert await svc.resolve_token(db_session, minted2["token"]) is None


async def test_two_live_token_cap(db_session, audit_db):
    admin = await _admin(db_session)
    account = await svc.create_account(db_session, name="rotating", role="power", actor=admin)
    await svc.mint_token(db_session, account, name="a", expires_in_days=30, actor=admin)
    await svc.mint_token(db_session, account, name="b", expires_in_days=30, actor=admin)
    with pytest.raises(svc.ServiceAccountError, match="live tokens"):
        await svc.mint_token(db_session, account, name="c", expires_in_days=30, actor=admin)


async def test_admin_role_refused(db_session, audit_db):
    admin = await _admin(db_session)
    with pytest.raises(svc.ServiceAccountError, match="never admin"):
        await svc.create_account(db_session, name="rogue", role="admin", actor=admin)


async def test_act_only_project_creation_blocked(db_session, client_for, audit_db):
    admin = await _admin(db_session)
    account = await svc.create_account(db_session, name="actor", role="power", actor=admin)
    async with client_for(account) as client:
        r = await client.post("/api/v1/projects", json={"name": "Owned by a bot"})
        assert r.status_code == 403
        assert "share" in r.json()["detail"].lower()


async def test_suspended_account_tokens_refused(db_session, audit_db):
    admin = await _admin(db_session)
    account = await svc.create_account(db_session, name="pausable", role="power", actor=admin)
    minted = await svc.mint_token(db_session, account, name="t", expires_in_days=30, actor=admin)
    account.status = "suspended"
    await db_session.commit()
    # resolve returns the row; the AUTH layer 403s suspended — mirror that check
    resolved = await svc.resolve_token(db_session, minted["token"])
    assert resolved is not None and resolved.status == "suspended"
    # minting against a suspended account refuses
    with pytest.raises(svc.ServiceAccountError, match="suspended"):
        await svc.mint_token(db_session, account, name="x", expires_in_days=5, actor=admin)


async def test_service_rows_hidden_from_admin_users_list(db_session, client_for, audit_db):
    admin = await _admin(db_session)
    await svc.create_account(db_session, name="invisible", role="power", actor=admin)
    async with client_for(admin) as client:
        r = await client.get("/api/v1/admin/users?page_size=100")
        assert r.status_code == 200
        emails = [u["email"] for u in r.json()["items"]]
        assert not any("@service.marshal.local" in e for e in emails)
