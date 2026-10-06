"""Deployment lifecycle ticks (deployment-maturity spec R1/R5).

Health probing + expiry sweeping, both idempotent and timestamp-derived (the
S6 pattern): restarts and double-fires are harmless. S12 wraps these in the
scheduler election; the ticks themselves stay correctness-independent of it.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.models import Deployment, User

logger = logging.getLogger("marshal.deploy")

HEALTH_STRIKES = 3
HEALTH_TIMEOUT_S = 10
EXPIRY_WARNINGS_H = (48, 24)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # sqlite naive datetimes
        return dt.replace(tzinfo=UTC)
    return dt


def _health_url(deployment: Deployment) -> str | None:
    if not deployment.app_url:
        return None
    base = deployment.app_url.rstrip("/")
    path = (deployment.health_path or "/").lstrip("/")
    return f"{base}/{path}" if path else base


async def _probe(url: str) -> tuple[bool, int | None]:
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_S) as client:
            response = await client.get(url)
            return response.status_code < 500, response.status_code
    except httpx.HTTPError:
        return False, None


def _recent_probe_failures(timeline: list) -> int:
    """Consecutive probe failures derived from the timeline tail (no counters)."""
    count = 0
    for entry in reversed(timeline or []):
        phase = entry.get("phase")
        if phase == "health_probe_failed":
            count += 1
        elif phase == "health_probe_ok":
            break
        elif phase not in ("health_degraded", "health_recovered"):
            break
    return count


async def health_tick() -> None:
    """Probe every active deployment; 3 strikes → degraded; success → recovery
    (R1). Pure observer — no restarts, no remediation."""
    from app.core.db import SessionLocal

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(Deployment).where(Deployment.status == "active")))
            .scalars().all()
        )
        for deployment in rows:
            url = _health_url(deployment)
            if url is None:
                continue
            ok, code = await _probe(url)
            deployment.last_health_at = _now()
            timeline = list(deployment.timeline or [])
            if ok:
                if deployment.health == "degraded":
                    timeline.append({"phase": "health_recovered", "at": _now().isoformat(),
                                     "detail": f"HTTP {code}"})
                    deployment.health = "healthy"
                    logger.info("deployment %s health recovered", deployment.id)
                elif deployment.health != "healthy":
                    deployment.health = "healthy"
                timeline.append({"phase": "health_probe_ok", "at": _now().isoformat(),
                                 "detail": f"HTTP {code}"})
            else:
                timeline.append({"phase": "health_probe_failed", "at": _now().isoformat(),
                                 "detail": f"HTTP {code}" if code else "no response"})
                failures = _recent_probe_failures(timeline)
                if failures >= HEALTH_STRIKES and deployment.health != "degraded":
                    deployment.health = "degraded"
                    incident_start = timeline[-HEALTH_STRIKES].get("at", _now().isoformat())
                    timeline.append({"phase": "health_degraded", "at": _now().isoformat(),
                                     "detail": f"{failures} consecutive probe failures"})
                    from app.services import notifications as notif

                    owner = await db.get(User, deployment.user_id)
                    if owner is not None:
                        await notif.notify(
                            db, owner,
                            type="deployment_degraded",
                            title="Deployment health degraded",
                            body=(
                                f"{url} failed {failures} consecutive health probes"
                                + (f" (last HTTP {code})" if code else " (no response)")
                            ),
                            link=f"/projects/{deployment.project_id}",
                            dedupe_key=f"degraded:{deployment.id}:{incident_start}",
                        )
            # Keep the timeline bounded: retain phases, trim probe noise
            probes = [e for e in timeline if e.get("phase", "").startswith("health_probe")]
            if len(probes) > 12:
                drop = set(id(e) for e in probes[:-12])
                timeline = [e for e in timeline if id(e) not in drop]
            deployment.timeline = timeline
            await db.commit()


async def expiry_tick() -> None:
    """Warn at 48h/24h, tear down at expiry through the STANDARD path (R5).
    Legacy rows with expires_at NULL are grandfathered (never auto-expired)."""
    from app.core.db import SessionLocal
    from app.services import audit
    from app.services import deployment as deploy_svc
    from app.services import notifications as notif

    async with SessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Deployment).where(
                        Deployment.status == "active",
                        Deployment.expires_at.is_not(None),
                    )
                )
            ).scalars().all()
        )
        now = _now()
        for deployment in rows:
            expires_at = _aware(deployment.expires_at)
            owner = await db.get(User, deployment.user_id)
            if expires_at <= now:
                audit.emit(
                    audit.write_entry(
                        actor_id=None, category=audit.DEPLOYMENT, action="deployment_expired",
                        resource_type="deployment", resource_id=str(deployment.id),
                        project_id=deployment.project_id,
                        detail={"expired_at": expires_at.isoformat()},
                    )
                )
                if owner is not None:
                    await notif.notify(
                        db, owner,
                        type="deployment_expired",
                        title="Deployment expired — tearing down",
                        body="Its TTL elapsed; the Enclave stack is being removed. "
                             "Redeploy any ready build whenever you need it back.",
                        link=f"/projects/{deployment.project_id}",
                        dedupe_key=f"expired:{deployment.id}",
                    )
                try:
                    await deploy_svc.start_teardown(db, deployment, force=True)
                except ValueError:
                    logger.info("expiry teardown raced a state change for %s", deployment.id)
                continue
            for threshold in EXPIRY_WARNINGS_H:
                window = expires_at - timedelta(hours=threshold)
                if window <= now and owner is not None:
                    await notif.notify(
                        db, owner,
                        type="deployment_expiring",
                        title=f"Deployment expires in under {threshold}h",
                        body="Extend it from the deployment card if you still need it.",
                        link=f"/projects/{deployment.project_id}",
                        dedupe_key=f"expiring:{deployment.id}:{threshold}",
                    )
                    break  # only the nearest threshold fires per tick
