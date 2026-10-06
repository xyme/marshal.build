"""Cognito JWT validation + JIT user provisioning (foundation spec R4)."""

import logging
from dataclasses import dataclass
from datetime import UTC

import jwt
from fastapi import Depends, HTTPException, Request
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.models import User

logger = logging.getLogger("marshal.auth")

ROLE_PRECEDENCE = ["admin", "power", "business"]

_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(get_settings().cognito_jwks_url, cache_keys=True, lifespan=3600)
    return _jwks_client


@dataclass
class TokenClaims:
    sub: str
    username: str
    groups: list[str]
    role: str
    amr: list[str]  # authentication methods (S14-02: MFA detection)
    raw: dict  # verified claim dict (tenant resolution reads custom:* claims)

    @property
    def mfa_satisfied(self) -> bool:
        """amr-marker scan — FORWARD-COMPATIBLE DISJUNCT ONLY (FSD §13.5M).

        Cognito user-pool access tokens carry NO amr claim today (verified
        live, 7 Sep 2026), so this alone can never pass in production. It
        stays as one arm of `session_mfa_satisfied` in case Cognito ever
        emits MFA markers ("mfa", "swmfa", "software_token_mfa", "sms_mfa").
        Gate consumers must call `session_mfa_satisfied`, not this.
        """
        return any("mfa" in method.lower() for method in self.amr)


def session_mfa_satisfied(claims: "TokenClaims | None", user: "User | None") -> bool:
    """True when THIS session authenticated with a second factor.

    Primary signal is temporal (S14-02 fix, FSD §13.5M): Cognito always
    challenges a user whose preferred factor is set, so any session whose
    `auth_time` is at/after the platform-recorded TOTP confirmation
    necessarily passed the challenge. Pre-enrollment sessions correctly
    fail. The amr scan remains as a forward-compatible disjunct. Service
    accounts (raw={}) and absent claims resolve False.
    """
    if claims is None:
        return False
    if claims.mfa_satisfied:  # amr marker, if Cognito ever sends one
        return True
    if user is None or user.mfa_enrolled_at is None:
        return False
    auth_time = claims.raw.get("auth_time")
    if not isinstance(auth_time, (int, float)) or isinstance(auth_time, bool):
        return False
    enrolled = user.mfa_enrolled_at
    if enrolled.tzinfo is None:  # sqlite (tests) returns naive UTC
        enrolled = enrolled.replace(tzinfo=UTC)
    # auth_time is whole seconds; floor the enrollment instant to match its
    # granularity, else a same-second sign-in fails on lost microseconds.
    return float(auth_time) >= int(enrolled.timestamp())


def resolve_role(groups: list[str]) -> str:
    for role in ROLE_PRECEDENCE:
        if role in groups:
            return role
    return "business"


# Cognito prefixes federated usernames with the lowercased provider name
# ("entraid_<subject>", "okta_<subject>"). Known providers per D1 (S15-01).
KNOWN_IDP_PREFIXES = ("entraid", "okta")


def provider_from_username(username: str) -> str:
    """'entraid' | 'okta' | 'native' — provenance for audit evidence."""
    prefix = (username or "").split("_", 1)[0].lower()
    return prefix if prefix in KNOWN_IDP_PREFIXES else "native"


def validate_token(token: str) -> TokenClaims:
    """Validate a Cognito access token: signature, expiry, token_use, client_id."""
    settings = get_settings()
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    if claims.get("token_use") != "access":
        raise HTTPException(status_code=401, detail="Wrong token type")
    if claims.get("client_id") != settings.cognito_client_id:
        raise HTTPException(status_code=401, detail="Token not issued for this client")

    groups = claims.get("cognito:groups", []) or []
    amr = claims.get("amr", []) or []
    return TokenClaims(
        sub=claims["sub"],
        username=claims.get("username", ""),
        groups=groups,
        role=resolve_role(groups),
        amr=[str(method) for method in amr],
        raw=claims,
    )


async def admin_mfa_required() -> bool:
    """Return the trusted runtime MFA policy.

    The policy itself still ships OFF until administrators are enrolled. When
    fail-closed mode is enabled, an unavailable security snapshot propagates
    so privileged routes return the dedicated 503 instead of bypassing MFA.
    """
    from app.services.platform_settings import get_security_controls

    return bool((await get_security_controls()).get("admin_mfa_required"))


async def enforce_admin_security(request: Request) -> None:
    """Resolve trusted controls and enforce configured MFA for an admin.

    Both admin-only routers and shared routes with an admin authorization
    bypass call this helper so their fail-closed and MFA behavior cannot drift.
    """
    # Resolve policy for every privileged request, even when this token already
    # proves MFA. That establishes a trusted snapshot and lets fail-closed mode
    # surface the dedicated 503 on a cold read failure.
    required = await admin_mfa_required()
    claims: TokenClaims | None = getattr(request.state, "auth_claims", None)
    user: User | None = getattr(request.state, "user", None)
    if required and not session_mfa_satisfied(claims, user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "admin_mfa_required",
                "detail": "Administrator actions require multi-factor "
                "authentication. Set up an authenticator app in your "
                "profile, then sign in again.",
            },
        )


def require_role(*roles: str):
    """Dependency factory: 403 unless the user's role is in `roles`.

    Admin routes additionally require a second factor when
    `security.admin_mfa_required` is on (S14-02, product decision D8).
    """

    async def checker(request: Request, user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            # View-only admin visibility (product decision, 4 Sep 2026): a
            # flagged human account may READ any admin surface under the same
            # MFA fail-closed bar as a real administrator, but never mutate.
            # The strict method rule also keeps semantically-read POST
            # endpoints (exports, integration test-sends, probes) admin-only.
            if "admin" in roles and user.admin_readonly:
                if request.method in ("GET", "HEAD"):
                    await enforce_admin_security(request)
                    return user
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "admin_read_only",
                        "detail": "Your admin console access is view-only — "
                        "actions are disabled for beta viewer accounts.",
                    },
                )
            # S15-05 error-copy audit: say what is needed and who can grant it,
            # not just that the door is shut.
            needed = " or ".join(sorted(roles))
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This area needs the {needed} role. Your account has "
                    f"'{user.role}' — ask a platform administrator if you need access."
                ),
            )
        if user.role == "admin" and "admin" in roles:
            await enforce_admin_security(request)
        return user

    return checker


def require_editor_persona():
    """Editing capabilities are Power-User surface area (FSD §4.4.2)."""

    async def checker(
        request: Request, user: User = Depends(get_current_user)
    ) -> User:
        if user.role == "admin":
            await enforce_admin_security(request)
            return user
        if user.persona == "power":
            return user
        raise HTTPException(
            status_code=403,
            detail="Spec editing is available in the Power User experience. "
            "Switch persona in your profile (may require admin approval).",
        )

    return checker


async def _email_from_cognito(access_token: str) -> str | None:
    """Verified email for JIT provisioning (pre-Beta hardening item 4).

    GetUser authenticates BY the access token itself (user-context API, same
    seam the S14-02 MFA gate uses) — the email comes from Cognito, not from a
    spoofable request header. Called once per user, ever (JIT only)."""
    import asyncio

    import boto3

    def _fetch() -> str | None:
        client = boto3.client("cognito-idp", region_name=get_settings().aws_region)
        attrs = client.get_user(AccessToken=access_token).get("UserAttributes", [])
        return next((a["Value"] for a in attrs if a["Name"] == "email"), None)

    try:
        return await asyncio.to_thread(_fetch)
    except Exception:  # noqa: BLE001 — provisioning must not depend on this call
        logger.warning("Cognito GetUser email lookup failed at JIT", exc_info=True)
        return None


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """FastAPI dependency: validate bearer token, JIT-provision the user row."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    bearer = auth_header.split(" ", 1)[1].strip()

    # B9 service-account tokens (integration-wave R2.2): prefix-matched BEFORE
    # any Cognito/JWKS work. The resolved row satisfies the same downstream
    # contracts — tenant seam reads raw={} (→ platform tenant), the MFA gate
    # reads amr=[] (irrelevant: admin tokens do not exist by product decision),
    # suspension applies, caps/limits/audit key on the user id as always.
    if bearer.startswith("mat_"):
        from app.services.service_accounts import resolve_token

        service_user = await resolve_token(db, bearer)
        if service_user is None:
            raise HTTPException(status_code=401, detail="Invalid, expired or revoked token")
        if service_user.status == "suspended":
            raise HTTPException(
                status_code=403, detail="Account suspended. Contact your administrator."
            )
        request.state.token_claims = {}
        request.state.auth_claims = TokenClaims(
            sub=service_user.cognito_sub,
            username=service_user.name or "service-account",
            groups=[service_user.role],
            role=service_user.role,
            amr=[],
            raw={},
        )
        request.state.user = service_user
        return service_user

    claims = validate_token(bearer)
    # Two shapes on purpose: `token_claims` is the raw verified dict the tenant
    # seam reads (custom:* claims, S12), `auth_claims` is the typed view the
    # MFA gate reads (S14-02). Keeping the dict contract avoids breaking
    # tenant resolution.
    request.state.token_claims = claims.raw
    request.state.auth_claims = claims

    result = await db.execute(select(User).where(User.cognito_sub == claims.sub))
    user = result.scalar_one_or_none()
    if user is None:
        # Email comes from Cognito (verified), NOT from the x-user-email
        # header the proxy used to send — any direct token-holder path could
        # spoof that header and plant a victim address on the row (which the
        # notification email channel would then mail). Header trust removed.
        email = await _email_from_cognito(bearer) or f"{claims.username}@unknown"
        # Display name comes from the user later (access tokens carry no name claim);
        # default to the email local part so the UI has something friendly.
        friendly = email.split("@")[0].replace(".", " ").title() if "@" in email else None
        user = User(
            cognito_sub=claims.sub,
            email=email,
            name=friendly,
            role=claims.role,
            persona="power" if claims.role in ("power", "admin") else None,
            account_class="standard",
            experience_view=None,
        )
        db.add(user)
        created = False
        try:
            await db.commit()
            await db.refresh(user)
            created = True
        except IntegrityError:
            # Two live tasks can receive the same first authenticated requests
            # concurrently. The unique cognito_sub is the arbiter: the loser
            # rolls back and reuses the winner instead of returning a 500.
            await db.rollback()
            user = (
                await db.execute(select(User).where(User.cognito_sub == claims.sub))
            ).scalar_one_or_none()
            if user is None:
                raise
        provider = provider_from_username(claims.username)
        if created:
            logger.info(
                "JIT-provisioned user %s role=%s provider=%s",
                user.email,
                user.role,
                provider,
            )
        if created and provider != "native":
            # S15-01: first federated sign-in is a SECURITY event — the row is
            # the evidence that enterprise SSO provisioned this account, with
            # which role, from which IdP, on which day.
            from app.services import audit

            audit.emit(
                audit.write_entry(
                    actor_id=user.id,
                    category=audit.SECURITY,
                    action="user_federated_signin",
                    resource_type="user",
                    resource_id=str(user.id),
                    detail={"provider": provider, "role": user.role, "email": user.email},
                )
            )
    elif user.role != claims.role and user.role_source == "sso":
        # Role changes in Cognito take effect on next request (FSD §4.4.9 AC-4).
        # Admin-pinned roles (role_source='admin', S3-08) are authoritative until
        # the Cognito group mirror catches up in the user's token — never let a
        # stale claim undo an explicit admin decision.
        user.role = claims.role
        await db.commit()
        await db.refresh(user)
    if user.status == "suspended":
        # Cognito admin-disable blocks NEW tokens; this blocks tokens already
        # issued before suspension (project-admin-dashboard spec R4.2).
        raise HTTPException(status_code=403, detail="Account suspended. Contact your administrator.")
    request.state.user = user
    return user


async def require_admin_security_if_admin(
    request: Request, user: User = Depends(get_current_user)
) -> User:
    """Shared-router dependency: canonical admin security, non-admin no-op."""
    if user.role == "admin":
        await enforce_admin_security(request)
    return user
