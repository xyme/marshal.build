"""Admin user management (project-admin-dashboard spec R4, FSD §4.4.5).

Role changes are effective on the target's NEXT API call: we pin the local row
(role_source='admin') so the per-request claims sync in auth.get_current_user
stops overwriting it, and we mirror the Cognito group so hosted-UI claims align
after the user's next sign-in. Cognito failures roll the local change back —
no silent drift between the two stores.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AuditLog, Project, User

logger = logging.getLogger("marshal.admin_users")

ROLE_GROUPS = ("admin", "power", "business")


class AdminUserError(Exception):
    """Rule violation (self-demotion, bad transition) — surfaces as 422."""


class CognitoSyncError(Exception):
    """Cognito mirror failed — local change rolled back; surfaces as 502."""


def _cognito():
    return boto3.client("cognito-idp", region_name=get_settings().aws_region)


def _run_cognito(fn, **kwargs):
    try:
        return fn(**kwargs)
    except (ClientError, BotoCoreError) as exc:
        raise CognitoSyncError(str(exc)) from exc


async def user_stats(db: AsyncSession) -> dict:
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    by_persona = dict(
        (await db.execute(select(User.persona, func.count()).group_by(User.persona))).all()
    )
    admins = (
        await db.execute(select(func.count()).select_from(User).where(User.role == "admin"))
    ).scalar_one()
    since = datetime.now(UTC) - timedelta(days=30)
    active_30d = (
        await db.execute(
            select(func.count(func.distinct(AuditLog.actor_id))).where(
                AuditLog.created_at >= since, AuditLog.actor_id.is_not(None)
            )
        )
    ).scalar_one()
    return {
        "total": total,
        "active_30d": active_30d,
        "business": by_persona.get("business", 0),
        "power": by_persona.get("power", 0),
        "admins": admins,
    }


async def list_users(
    db: AsyncSession,
    *,
    q: str | None = None,
    role: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    # B9: the human-facing admin list excludes service rows by default —
    # service accounts have their own surface (integration-wave R2.4).
    stmt = select(User).where(User.kind != "service")
    if q:
        like = f"%{q}%"
        stmt = stmt.where(User.email.ilike(like) | User.name.ilike(like))
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    result = await db.execute(
        stmt.order_by(User.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    )
    users = list(result.scalars())
    ids = [u.id for u in users]
    project_counts: dict[uuid.UUID, int] = {}
    last_active: dict[uuid.UUID, datetime] = {}
    if ids:
        rows = await db.execute(
            select(Project.user_id, func.count())
            .where(Project.user_id.in_(ids), Project.status != "deleted")
            .group_by(Project.user_id)
        )
        project_counts = dict(rows.all())
        rows = await db.execute(
            select(AuditLog.actor_id, func.max(AuditLog.created_at))
            .where(AuditLog.actor_id.in_(ids))
            .group_by(AuditLog.actor_id)
        )
        last_active = dict(rows.all())
    items = [
        {
            "user": u,
            "project_count": project_counts.get(u.id, 0),
            "last_active_at": last_active.get(u.id),
        }
        for u in users
    ]
    return items, total


async def update_user(
    db: AsyncSession,
    target: User,
    acting_admin: User,
    *,
    role: str | None = None,
    persona: str | None = None,
    status: str | None = None,
    budget_override_usd: float | None = None,
    clear_budget: bool = False,
    account_class: str | None = None,
    experience_view: str | None = None,
    clear_experience_view: bool = False,
    admin_readonly: bool | None = None,
) -> tuple[User, dict]:
    """Apply local changes, mirroring only real access changes to Cognito."""
    if target.kind != "human":
        raise AdminUserError("Demo experience settings apply only to human users")
    if target.id == acting_admin.id and (
        (role and role != "admin") or status == "suspended"
    ):
        raise AdminUserError("You cannot demote or suspend your own account")
    if clear_experience_view and experience_view is not None:
        raise AdminUserError("Choose an experience view or clear it, not both")

    effective_account_class = account_class or target.account_class
    if experience_view is not None and effective_account_class != "demo":
        raise AdminUserError("Experience Preview can be initialized only for demo accounts")

    before = {
        "role": target.role,
        "persona": target.persona,
        "status": target.status,
        "budget_override_usd": (
            float(target.budget_override_usd)
            if target.budget_override_usd is not None
            else None
        ),
        "account_class": target.account_class,
        "experience_view": target.experience_view,
        "admin_readonly": target.admin_readonly,
    }
    settings = get_settings()
    pool_id = settings.cognito_user_pool_id
    cognito_ops: list[tuple] = []

    if role and role != target.role:
        target.role = role
        target.role_source = "admin"
        # Mirror group membership so future tokens carry the right claim.
        for group in ROLE_GROUPS:
            cognito_ops.append(
                ("remove_group", dict(UserPoolId=pool_id, Username=target.cognito_sub, GroupName=group))
            )
        cognito_ops.append(
            ("add_group", dict(UserPoolId=pool_id, Username=target.cognito_sub, GroupName=role))
        )
    if persona and persona != target.persona:
        target.persona = persona
        target.persona_upgrade_requested = False
    if status and status != target.status:
        target.status = status
        cognito_ops.append(
            (
                "disable" if status == "suspended" else "enable",
                dict(UserPoolId=pool_id, Username=target.cognito_sub),
            )
        )
        if status == "suspended":
            # S15-03 session invalidation: disable only blocks NEW tokens.
            # Global sign-out revokes the refresh tokens, so sessions already
            # in flight cannot renew past their (1 h) access-token expiry —
            # and the API blocks even that hour via the suspended check in
            # auth.get_current_user. Both halves together are the drill claim.
            cognito_ops.append(
                ("global_sign_out", dict(UserPoolId=pool_id, Username=target.cognito_sub))
            )
    if clear_budget:
        target.budget_override_usd = None
    elif budget_override_usd is not None:
        target.budget_override_usd = budget_override_usd

    # Demo classification and view are database-only presentation metadata.
    # They intentionally produce no Cognito operations and never alter role,
    # role_source, or persona.
    if account_class is not None:
        target.account_class = account_class
        if account_class == "standard":
            target.experience_view = None
    if clear_experience_view:
        target.experience_view = None
    elif experience_view is not None:
        target.experience_view = experience_view
    # View-only admin visibility (owner decision, 4 Sep 2026): capability
    # metadata like the demo fields above — database-only, no Cognito
    # operation, no effect on role, role_source, or persona. Enforcement
    # (GET/HEAD + admin-MFA parity) lives in core/auth.require_role.
    if admin_readonly is not None:
        target.admin_readonly = admin_readonly

    client = _cognito() if cognito_ops else None
    try:
        for op, kwargs in cognito_ops:
            if op == "remove_group":
                # Removing from a group the user isn't in raises — tolerate that one case.
                try:
                    client.admin_remove_user_from_group(**kwargs)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                        raise CognitoSyncError(str(exc)) from exc
            elif op == "add_group":
                _run_cognito(client.admin_add_user_to_group, **kwargs)
            elif op == "disable":
                _run_cognito(client.admin_disable_user, **kwargs)
            elif op == "enable":
                _run_cognito(client.admin_enable_user, **kwargs)
            elif op == "global_sign_out":
                _run_cognito(client.admin_user_global_sign_out, **kwargs)
    except CognitoSyncError:
        await db.rollback()
        raise
    await db.commit()
    await db.refresh(target)
    after = {
        "role": target.role,
        "persona": target.persona,
        "status": target.status,
        "budget_override_usd": (
            float(target.budget_override_usd)
            if target.budget_override_usd is not None
            else None
        ),
        "account_class": target.account_class,
        "experience_view": target.experience_view,
        "admin_readonly": target.admin_readonly,
    }
    return target, {"before": before, "after": after}


async def decide_persona_request(
    db: AsyncSession, target: User, *, approve: bool
) -> tuple[User, dict]:
    if not target.persona_upgrade_requested:
        raise AdminUserError("User has no pending persona upgrade request")
    before = {"persona": target.persona, "persona_upgrade_requested": True}
    if approve:
        target.persona = "power"
    target.persona_upgrade_requested = False
    await db.commit()
    await db.refresh(target)
    return target, {
        "before": before,
        "after": {"persona": target.persona, "persona_upgrade_requested": False},
        "approved": approve,
    }


# ------------------------------------------- S15-03 lifecycle & access review

DORMANT_DAYS_DEFAULT = 90


async def offboard_user(db: AsyncSession, target: User, acting_admin: User) -> dict:
    """Permanent access removal, distinct from suspension.

    Suspension is REVERSIBLE: status flips, Cognito disables, and every
    membership is preserved so re-enabling restores the person exactly. This is
    the irreversible counterpart an enterprise means by offboarding:

      1. suspend (local status + Cognito disable + global sign-out, via
         update_user's mirror — sign-out repeated here if already suspended),
      2. strip every team membership,
      3. strip every explicit project membership,
      4. REPORT owned projects instead of touching them.

    Step 4 is deliberate. Deleting or auto-transferring someone's projects on
    their last day destroys work or hands it to the wrong person; the admin gets
    the list and decides. Ownership transfer already exists (collaboration R4).
    """
    from app.models import ProjectMember
    from app.services import teams as teams_svc

    if target.id == acting_admin.id:
        raise AdminUserError("You cannot offboard your own account")

    if target.status != "suspended":
        target, _change = await update_user(db, target, acting_admin, status="suspended")
    else:
        # Already suspended (possibly long ago, before suspend revoked
        # sessions): offboarding must still guarantee no live session survives.
        # Idempotent, so re-running an offboard is safe.
        _run_cognito(
            _cognito().admin_user_global_sign_out,
            UserPoolId=get_settings().cognito_user_pool_id,
            Username=target.cognito_sub,
        )

    teams_removed = await teams_svc.remove_user_everywhere(db, target.id)

    memberships = list(
        (
            await db.execute(
                select(ProjectMember).where(ProjectMember.user_id == target.id)
            )
        ).scalars()
    )
    for membership in memberships:
        await db.delete(membership)
    if memberships:
        await db.commit()

    owned = list(
        (
            await db.execute(
                select(Project.id, Project.name).where(
                    Project.user_id == target.id, Project.status != "deleted"
                )
            )
        ).all()
    )
    logger.info(
        "offboarded %s: teams=%d project_shares=%d owned_needing_transfer=%d",
        target.email, teams_removed, len(memberships), len(owned),
    )
    return {
        "user_id": str(target.id),
        "email": target.email,
        "status": target.status,
        "teams_removed": teams_removed,
        "project_shares_removed": len(memberships),
        "owned_projects_needing_transfer": [
            {"id": str(pid), "name": name} for pid, name in owned
        ],
    }


async def access_review(db: AsyncSession, *, dormant_days: int = DORMANT_DAYS_DEFAULT) -> dict:
    """Who has what access, for periodic review (S15-03).

    `last_active_at` is derived from the audit trail rather than a column
    updated on every request: the trail is already written per request, so
    deriving avoids a write on the hot path and cannot drift from the evidence.
    """
    from app.models import Team, TeamMember

    users = list((await db.execute(select(User).order_by(User.email))).scalars())
    ids = [u.id for u in users]

    last_active: dict[uuid.UUID, datetime] = {}
    project_counts: dict[uuid.UUID, int] = {}
    team_map: dict[uuid.UUID, list[str]] = {}
    if ids:
        last_active = dict(
            (
                await db.execute(
                    select(AuditLog.actor_id, func.max(AuditLog.created_at))
                    .where(AuditLog.actor_id.in_(ids))
                    .group_by(AuditLog.actor_id)
                )
            ).all()
        )
        project_counts = dict(
            (
                await db.execute(
                    select(Project.user_id, func.count())
                    .where(Project.user_id.in_(ids), Project.status != "deleted")
                    .group_by(Project.user_id)
                )
            ).all()
        )
        rows = await db.execute(
            select(TeamMember.user_id, Team.name, TeamMember.role)
            .join(Team, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id.in_(ids))
        )
        for user_id, team_name, team_role in rows.all():
            team_map.setdefault(user_id, []).append(f"{team_name}:{team_role}")

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=dormant_days)
    rows_out = []
    dormant = 0
    for user in users:
        seen = last_active.get(user.id)
        if seen is not None and seen.tzinfo is None:  # sqlite naive
            seen = seen.replace(tzinfo=UTC)
        is_dormant = user.status == "active" and (seen is None or seen < cutoff)
        if is_dormant:
            dormant += 1
        rows_out.append(
            {
                "email": user.email,
                "name": user.name,
                "platform_role": user.role,
                "role_source": user.role_source,
                "persona": user.persona,
                "status": user.status,
                "teams": sorted(team_map.get(user.id, [])),
                "owned_projects": project_counts.get(user.id, 0),
                "last_active_at": seen.isoformat() if seen else None,
                "dormant": is_dormant,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            }
        )
    return {
        "generated_at": now.isoformat(),
        "dormant_threshold_days": dormant_days,
        "user_count": len(rows_out),
        "dormant_count": dormant,
        "admin_count": sum(1 for r in rows_out if r["platform_role"] == "admin"),
        "users": rows_out,
    }
