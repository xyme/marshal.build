"""Sandbox spend poller (cost-caps-alerts spec R6) — dark until Cost Explorer
is enabled in the management account (owner action; see docs runbook).

Daily upsert of per-lease spend from CE GetCostAndUsage via the MarshalCostReader
cross-account role. Failure = logged, feature stays dark; nothing else depends on it.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import boto3
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Lease, SandboxSpend

logger = logging.getLogger("marshal.sandbox_costs")

POLL_INTERVAL_S = 6 * 3600  # 4x daily is plenty against CE's ~24h lag


def _aware(dt: datetime | None) -> datetime | None:
    """sqlite hands back naive datetimes; CE arithmetic needs aware ones."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _ce_client():
    """Cost Explorer client via the management-account MarshalCostReader role."""
    # B20 R0.6: read through Settings (pydantic maps COST_READER_ROLE_ARN),
    # not a raw os.environ scattered outside the config surface.
    role_arn = get_settings().cost_reader_role_arn
    if not role_arn:
        raise RuntimeError("COST_READER_ROLE_ARN not configured")
    sts = boto3.client("sts", region_name=get_settings().aws_region)
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="marshal-cost-reader")[
        "Credentials"
    ]
    return boto3.client(
        "ce",
        region_name="us-east-1",  # CE is us-east-1 only
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


async def poll_once() -> int:
    """Upsert daily spend for accounts with leases active in the last 45 days."""
    from app.core.db import SessionLocal

    upserts = 0
    async with SessionLocal() as db:
        since = datetime.now(UTC) - timedelta(days=45)
        leases = (
            await db.execute(
                select(Lease).where(
                    Lease.aws_account_id.is_not(None), Lease.requested_at >= since
                )
            )
        ).scalars().all()
        if not leases:
            return 0
        account_to_leases: dict[str, list[Lease]] = {}
        for lease in leases:
            account_to_leases.setdefault(lease.aws_account_id, []).append(lease)

        client = await asyncio.to_thread(_ce_client)
        end = datetime.now(UTC).date()
        start = end - timedelta(days=14)
        response = await asyncio.to_thread(
            client.get_cost_and_usage,
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
        )
        for bucket in response.get("ResultsByTime", []):
            day = datetime.fromisoformat(bucket["TimePeriod"]["Start"]).replace(tzinfo=UTC)
            day_end = day + timedelta(days=1)
            for group in bucket.get("Groups", []):
                account_id = group["Keys"][0]
                if account_id not in account_to_leases:
                    continue
                usd = float(group["Metrics"]["UnblendedCost"]["Amount"])
                # Attribute the day's spend to the lease that OVERLAPS that day
                # the most. Overlap (not containment): leases shorter than a day
                # — every drill lease, and most real ones — never contain a
                # day boundary, so a containment test attributed nothing at all
                # (found when the CE flag went live in S13).
                best_lease, best_overlap = None, timedelta(0)
                for lease in account_to_leases[account_id]:
                    lease_start = _aware(lease.activated_at or lease.requested_at)
                    lease_end = _aware(lease.terminated_at) or datetime.now(UTC)
                    overlap = min(day_end, lease_end) - max(day, lease_start)
                    if overlap > best_overlap:
                        best_lease, best_overlap = lease, overlap
                if best_lease is None:
                    continue
                existing = (
                    await db.execute(
                        select(SandboxSpend).where(
                            SandboxSpend.lease_id == best_lease.id,
                            SandboxSpend.date == day,
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    existing.usd = usd
                else:
                    db.add(
                        SandboxSpend(
                            lease_id=best_lease.id, project_id=best_lease.project_id,
                            date=day, usd=usd,
                        )
                    )
                upserts += 1
        await db.commit()
    # Logged HERE (not in the caller): S12 moved this poll under the scheduler
    # election, which replaced the old loop's result log — the poll's outcome
    # must stay visible wherever it is driven from.
    logger.info("sandbox spend poll: %d upserts across %d lease(s)", upserts, len(leases))
    return upserts


async def sandbox_spend_loop() -> None:
    while True:
        try:
            count = await poll_once()
            logger.info("sandbox spend poll: %d upserts", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — feature stays dark on failure (R6.2)
            logger.exception("sandbox spend poll failed")
        await asyncio.sleep(POLL_INTERVAL_S)
