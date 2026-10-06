"""B9 service accounts (integration-wave spec R2).

A service account IS a user row (`kind='service'`) — collaboration, audit,
caps, rate limits and tenancy work unchanged because the identity flows
through the same `User` everywhere. Tokens are the only new machinery:
`mat_`-prefixed bearer values, SHA-256 at rest, value shown exactly once,
mandatory expiry ≤365d, ≤2 live per account (rotation), instant revocation.

Decisions (owner, 24 Aug 2026): act-only (no project ownership) and NO
admin-scope tokens — a static credential must not carry the platform's most
powerful role.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ServiceAccountToken, User

TOKEN_PREFIX = "mat_"  # marshal api token
MAX_LIVE_TOKENS = 2
MAX_EXPIRY_DAYS = 365
ALLOWED_ROLES = ("business", "power")  # never admin (owner decision)


class ServiceAccountError(Exception):
    """Invalid operation — surfaces as 422."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


async def create_account(
    db: AsyncSession, *, name: str, role: str, actor: User
) -> User:
    name = (name or "").strip()
    if not (3 <= len(name) <= 60):
        raise ServiceAccountError("Name must be 3-60 characters")
    if role not in ALLOWED_ROLES:
        raise ServiceAccountError(
            f"Service accounts may hold {' or '.join(ALLOWED_ROLES)} — never admin"
        )
    slug = name.lower().replace(" ", "-")[:40]
    account = User(
        cognito_sub=f"svc-{uuid.uuid4().hex}",  # synthetic — no Cognito identity
        email=f"{slug}-{uuid.uuid4().hex[:6]}@service.marshal.local",
        name=name,
        role=role,
        role_source="admin",  # SSO role-sync must never touch service rows
        persona=None,
        kind="service",
        account_class="standard",
        experience_view=None,
        onboarding_completed=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def list_accounts(db: AsyncSession) -> list[dict]:
    accounts = (
        await db.execute(
            select(User).where(User.kind == "service").order_by(User.created_at.asc())
        )
    ).scalars().all()
    out = []
    for account in accounts:
        tokens = (
            await db.execute(
                select(ServiceAccountToken)
                .where(ServiceAccountToken.user_id == account.id)
                .order_by(ServiceAccountToken.created_at.desc())
            )
        ).scalars().all()
        out.append(
            {
                "id": str(account.id),
                "name": account.name,
                "role": account.role,
                "status": account.status,
                "created_at": account.created_at.isoformat(),
                "tokens": [
                    {
                        "id": str(t.id),
                        "name": t.name,
                        "prefix": f"{TOKEN_PREFIX}{t.token_prefix}…",
                        "expires_at": _aware(t.expires_at).isoformat(),
                        "revoked": t.revoked_at is not None,
                        "last_used_at": (
                            _aware(t.last_used_at).isoformat() if t.last_used_at else None
                        ),
                    }
                    for t in tokens
                ],
            }
        )
    return out


async def mint_token(
    db: AsyncSession, account: User, *, name: str, expires_in_days: int, actor: User
) -> dict:
    """Returns the token VALUE — the only time it ever exists in a response."""
    if account.kind != "service":
        raise ServiceAccountError("Tokens can only be minted for service accounts")
    if account.status != "active":
        raise ServiceAccountError("Account is suspended — reactivate it first")
    if not (1 <= expires_in_days <= MAX_EXPIRY_DAYS):
        raise ServiceAccountError(f"Expiry must be 1-{MAX_EXPIRY_DAYS} days")
    now = datetime.now(UTC)
    live = [
        t
        for t in (
            await db.execute(
                select(ServiceAccountToken).where(
                    ServiceAccountToken.user_id == account.id,
                    ServiceAccountToken.revoked_at.is_(None),
                )
            )
        ).scalars()
        if _aware(t.expires_at) > now
    ]
    if len(live) >= MAX_LIVE_TOKENS:
        raise ServiceAccountError(
            f"{MAX_LIVE_TOKENS} live tokens already exist — revoke one first "
            "(the two-token limit exists so rotation never needs a gap)"
        )
    value = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    row = ServiceAccountToken(
        user_id=account.id,
        name=(name or "token").strip()[:80],
        token_hash=_hash(value),
        token_prefix=value[len(TOKEN_PREFIX) : len(TOKEN_PREFIX) + 8],
        expires_at=now + timedelta(days=expires_in_days),
        created_by=actor.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": str(row.id),
        "token": value,  # shown once
        "expires_at": row.expires_at.isoformat(),
    }


async def revoke_token(db: AsyncSession, account: User, token_id: uuid.UUID) -> None:
    row = await db.get(ServiceAccountToken, token_id)
    if row is None or row.user_id != account.id:
        raise ServiceAccountError("Token not found on this account")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await db.commit()


async def resolve_token(db: AsyncSession, bearer: str) -> User | None:
    """The auth-path resolver (R2.2): hash lookup → live token → user row.
    Returns None for unknown/expired/revoked tokens (caller 401s)."""
    row = (
        await db.execute(
            select(ServiceAccountToken).where(
                ServiceAccountToken.token_hash == _hash(bearer)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(UTC)
    if _aware(row.expires_at) <= now:
        return None
    user = await db.get(User, row.user_id)
    if user is None or user.kind != "service":
        return None
    # last-used bookkeeping, throttled to one write per minute per token
    if row.last_used_at is None or (now - _aware(row.last_used_at)) > timedelta(minutes=1):
        row.last_used_at = now
        await db.commit()
    return user
