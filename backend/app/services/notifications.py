"""Notification emit seam + email channel (notifications spec, FSD §4.4.6).

Producers call notify()/notify_many() fire-and-forget via emit(); failures are
logged and never break the producing operation (same discipline as the audit
trail). In-app is the guaranteed channel; email rides SES behind EMAIL_ENABLED
(owner action: SES production access + DKIM — see docs runbook).
"""

import asyncio
import logging
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Notification, User

logger = logging.getLogger("marshal.notifications")

# FSD §4.4.6 defaults (event → {in_app, email}); stored prefs are sparse overrides.
DEFAULT_PREFS: dict[str, dict[str, bool]] = {
    "generation_complete": {"in_app": True, "email": False},
    "deploy_succeeded": {"in_app": True, "email": True},
    "deploy_failed": {"in_app": True, "email": True},
    "cost_threshold": {"in_app": True, "email": True},
    "cost_cap_reached": {"in_app": True, "email": True},
    "risk_review_requested": {"in_app": True, "email": True},
    "risk_decided": {"in_app": True, "email": True},
    "risk_changes_requested": {"in_app": True, "email": True},
    "risk_escalated": {"in_app": True, "email": True},
    "sample_published": {"in_app": True, "email": False},
    "project_shared": {"in_app": True, "email": True},
    # S7 collaboration + submissions
    "project_role_changed": {"in_app": True, "email": False},
    "project_unshared": {"in_app": True, "email": False},
    "comment_reply": {"in_app": True, "email": False},
    "ownership_transferred": {"in_app": True, "email": True},
    "submission_received": {"in_app": True, "email": False},
    "submission_decided": {"in_app": True, "email": True},
    # S8 codegen
    "build_ready": {"in_app": True, "email": False},
    "build_failed": {"in_app": True, "email": False},
    # S11 deployment lifecycle
    "deployment_degraded": {"in_app": True, "email": True},
    "deployment_expiring": {"in_app": True, "email": False},
    "deployment_expired": {"in_app": True, "email": False},
}

# Governance events reviewers cannot mute in-app (notifications spec R2.2).
FORCED_IN_APP = {"risk_review_requested", "risk_escalated"}

VALID_EVENT_KEYS = set(DEFAULT_PREFS)


def effective_prefs(user: User, event_type: str) -> dict[str, bool]:
    base = dict(DEFAULT_PREFS.get(event_type, {"in_app": True, "email": False}))
    override = (user.notification_prefs or {}).get(event_type, {})
    base.update({k: bool(v) for k, v in override.items() if k in ("in_app", "email")})
    return base


def _email_suppressed(user: User) -> str | None:
    settings = get_settings()
    if not getattr(settings, "email_enabled", False):
        return "email disabled"
    if user.status == "suspended":
        return "suspended user"
    if user.email.endswith("@marshal.demo"):
        return "demo mailbox"
    return None


def _ses_client():
    return boto3.client("sesv2", region_name=get_settings().aws_region)


def _render_email(title: str, body: str, link: str | None) -> tuple[str, str]:
    settings = get_settings()
    base = getattr(settings, "app_base_url", "http://localhost:3000")
    cta = f'{base}{link}' if link and link.startswith("/") else (link or base)
    html = f"""<html><body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:24px">
<div style="max-width:520px;margin:auto;background:#1e293b;border-radius:12px;padding:24px">
<h2 style="margin:0 0 8px">marshal</h2>
<h3 style="margin:0 0 12px">{title}</h3>
<p style="color:#94a3b8">{body}</p>
<p><a href="{cta}" style="display:inline-block;background:#6366f1;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none">Open in marshal</a></p>
<p style="font-size:12px;color:#64748b">Manage notification preferences in your profile: {base}/profile</p>
</div></body></html>"""
    text = f"marshal — {title}\n\n{body}\n\nOpen: {cta}\nPreferences: {base}/profile"
    return html, text


def _send_email(to: str, title: str, body: str, link: str | None) -> str:
    """Blocking SES send (runs in a thread). Returns email_status.

    S15-07 bounce/complaint handling rides SES's ACCOUNT-LEVEL suppression
    list (on by default for bounces + complaints): a hard bounce or complaint
    adds the address server-side, and later sends are rejected before leaving
    AWS. We classify that rejection as 'suppressed' (distinct from transient
    'failed') so the notification row records WHY the mail never arrived and
    the admin status surface can count it. No webhook needed — the backend has
    no unauthenticated public ingress for SNS to call (by design, S14).
    """
    settings = get_settings()
    sender = getattr(settings, "email_from", "marshal <no-reply@localhost>")
    html, text = _render_email(title, body, link)
    try:
        _ses_client().send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [to]},
            Content={
                "Simple": {
                    "Subject": {"Data": f"marshal — {title}"},
                    "Body": {"Html": {"Data": html}, "Text": {"Data": text}},
                }
            },
        )
        return "sent"
    except ClientError as exc:
        err = exc.response.get("Error", {})
        message = str(err.get("Message", ""))
        if err.get("Code") == "MessageRejected" and "suppress" in message.lower():
            logger.info("SES suppressed destination %s (bounce/complaint history)", to)
            return "suppressed"
        logger.warning("SES send failed to %s: %s", to, exc)
        return "failed"
    except BotoCoreError as exc:
        logger.warning("SES send failed to %s: %s", to, exc)
        return "failed"


def email_channel_status() -> dict:
    """Operator view of the email channel (S15-07): flag state, SES account
    status, sender identity/DKIM, and the suppression list the bounce and
    complaint handling relies on. Blocking boto3 — call via asyncio.to_thread.
    """
    settings = get_settings()
    out: dict = {
        "email_enabled": bool(getattr(settings, "email_enabled", False)),
        "email_from": getattr(settings, "email_from", ""),
    }
    client = _ses_client()
    try:
        account = client.get_account()
        out["production_access"] = bool(account.get("ProductionAccessEnabled"))
        out["sending_enabled"] = bool(account.get("SendingEnabled"))
        quota = account.get("SendQuota", {})
        out["quota"] = {
            "max_24h": quota.get("Max24HourSend"),
            "sent_24h": quota.get("SentLast24Hours"),
        }
        suppression = account.get("SuppressionAttributes", {})
        out["suppression_reasons"] = suppression.get("SuppressedReasons", [])
    except (ClientError, BotoCoreError) as exc:
        out["error"] = f"SES account lookup failed: {exc}"
        return out
    # Sender identity: DKIM must verify before the channel goes live (D2).
    domain = out["email_from"].split("@")[-1].rstrip(">").strip() if "@" in out["email_from"] else None
    if domain:
        try:
            identity = client.get_email_identity(EmailIdentity=domain)
            out["identity"] = {
                "domain": domain,
                "verified": bool(identity.get("VerifiedForSendingStatus")),
                "dkim_status": identity.get("DkimAttributes", {}).get("Status"),
            }
        except (ClientError, BotoCoreError):
            out["identity"] = {"domain": domain, "verified": False, "dkim_status": "NOT_FOUND"}
    try:
        page = client.list_suppressed_destinations(PageSize=100)
        entries = page.get("SuppressedDestinationSummaries", [])
        out["suppressed"] = {
            "count": len(entries),
            "truncated": "NextToken" in page,
            "recent": [
                {"email": e.get("EmailAddress"), "reason": e.get("Reason"),
                 "at": e.get("LastUpdateTime").isoformat() if e.get("LastUpdateTime") else None}
                for e in entries[:10]
            ],
        }
    except (ClientError, BotoCoreError) as exc:
        out["suppressed"] = {"error": str(exc)}
    return out


async def notify(
    db: AsyncSession,
    user: User,
    *,
    type: str,
    title: str,
    body: str = "",
    link: str | None = None,
    dedupe_key: str | None = None,
    force_in_app: bool = False,
) -> Notification | None:
    """Create one notification honoring prefs. Returns the row or None (muted/duped)."""
    # B10/B11 (integration-wave R3.2/R4.2): endpoint-scoped channels hook
    # BEFORE the per-user gates — subscriptions are not user prefs, and a
    # muted/deduped in-app row must not silence an integration. Both dedupe
    # per event (endpoint rows / in-task keys), so notify_many fans out once.
    # Fire-and-forget: failures never disturb the producing operation.
    _dispatch_integrations(
        type, title=title, body=body, link=link, dedupe_key=dedupe_key
    )
    prefs = effective_prefs(user, type)
    in_app = prefs["in_app"] or force_in_app or type in FORCED_IN_APP
    wants_email = prefs["email"]
    if not in_app and not wants_email:
        return None

    email_status = "skipped"
    suppression = _email_suppressed(user)
    if wants_email and suppression is None:
        email_status = "queued"

    row = Notification(
        user_id=user.id,
        type=type,
        title=title[:160],
        body=body,
        link=link,
        email_status=email_status,
        dedupe_key=dedupe_key,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:  # dedupe hit (partial unique) — by design, not an error
        await db.rollback()
        return None
    await db.refresh(row)

    if email_status == "queued":
        status = await asyncio.to_thread(_send_email, user.email, title, body, link)
        row.email_status = status
        await db.commit()
    return row


async def notify_many(
    db: AsyncSession, user_ids: list[uuid.UUID], **kwargs
) -> int:
    """Fan-out to a recipient list (reviewer groups); per-user dedupe keys keep
    the partial unique index meaningful."""
    count = 0
    base_dedupe = kwargs.pop("dedupe_key", None)
    for user_id in user_ids:
        user = await db.get(User, user_id)
        if user is None or user.status == "suspended":
            continue
        row = await notify(db, user, dedupe_key=base_dedupe, **kwargs)
        if row is not None:
            count += 1
    return count


def _dispatch_integrations(
    type: str, *, title: str, body: str, link: str | None, dedupe_key: str | None
) -> None:
    """B10 webhooks + B11 chat-ops off the notify() choke point (see notify)."""
    from app.services import chatops, webhooks

    emit(
        webhooks.dispatch(
            type, title=title, body=body, link=link, dedupe_key=dedupe_key
        )
    )
    emit(
        chatops.post(type, title=title, body=body, link=link, dedupe_key=dedupe_key)
    )


def emit(coro) -> None:
    """Fire-and-forget with a fresh session inside; never raises at the caller."""

    async def run() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.exception("notification emit failed")

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # pragma: no cover — scripts without a loop
        pass


async def emit_for_user(user_id: uuid.UUID, **kwargs) -> None:
    """Convenience producer path: fresh session, resolve user, notify."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        if user is not None:
            await notify(db, user, **kwargs)


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        )
    ).scalar_one()
