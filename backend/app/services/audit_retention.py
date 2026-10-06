"""Audit retention: archive to S3, then prune (security-hardening spec S14-06).

Policy (owner decision D9): **180 days hot** in Postgres, then archived to S3 as
newline-delimited JSON and deleted from the database. Archived objects are
keyed `audit-archive/<YYYY-MM-DD>/<batch>.jsonl` in the platform's own bucket.

Two invariants, both load-bearing:
  1. A row is NEVER deleted before `archived_at` is set — losing audit evidence
     to a retention bug is worse than keeping it too long.
  2. The tick is idempotent and resumable: archiving marks rows, pruning only
     touches marked rows, and a crash mid-batch simply repeats the batch.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import boto3
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.models import AuditLog

logger = logging.getLogger("marshal.audit_retention")

HOT_DAYS = 180
BATCH = 500
PREFIX = "audit-archive"


def _bucket() -> str:
    """Reuses the codegen workspace bucket (already private, TLS-enforced,
    SSE-S3). A dedicated bucket is a GA concern, not a security gap."""
    return get_settings().codegen_workspace_bucket


def _serialize(row: AuditLog) -> str:
    return json.dumps(
        {
            "id": str(row.id),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "category": row.category,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "project_id": str(row.project_id) if row.project_id else None,
            "detail": row.detail,
            "source_ip": getattr(row, "source_ip", None),
            "user_agent": getattr(row, "user_agent", None),
            "request_id": row.request_id,
            "http_status": row.http_status,
            "tenant_id": str(getattr(row, "tenant_id", "")) or None,
        },
        default=str,
    )


async def archive_tick() -> dict:
    """Archive-then-prune one pass. Returns {archived, pruned} counts."""
    from app.core.db import SessionLocal

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=HOT_DAYS)
    archived = pruned = 0

    if not settings.codegen_workspace_bucket:
        logger.info("audit retention: no bucket configured; skipping")
        return {"archived": 0, "pruned": 0}

    s3 = boto3.client("s3", region_name=settings.aws_region)
    async with SessionLocal() as db:
        # --- 1. archive unarchived rows past the hot window
        rows = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(AuditLog.created_at < cutoff, AuditLog.archived_at.is_(None))
                    .order_by(AuditLog.created_at.asc())
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        if rows:
            body = "\n".join(_serialize(row) for row in rows).encode()
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
            key = f"{PREFIX}/{rows[0].created_at.date().isoformat()}/{stamp}-{len(rows)}.jsonl"
            import asyncio

            await asyncio.to_thread(
                lambda: s3.put_object(
                    Bucket=_bucket(), Key=key, Body=body, ContentType="application/x-ndjson"
                )
            )
            now = datetime.now(UTC)
            for row in rows:
                row.archived_at = now
            await db.commit()
            archived = len(rows)
            logger.info("audit retention: archived %d rows to s3://%s/%s", archived, _bucket(), key)

        # --- 2. prune rows that ARE archived and past the window.
        # execution_options(synchronize_session=False): the default in-Python
        # evaluation compares naive sqlite datetimes with our aware cutoff and
        # raises; the database does this comparison correctly.
        result = await db.execute(
            delete(AuditLog)
            .where(AuditLog.created_at < cutoff, AuditLog.archived_at.is_not(None))
            .execution_options(synchronize_session=False)
        )
        pruned = result.rowcount or 0
        await db.commit()
        if pruned:
            logger.info("audit retention: pruned %d archived rows", pruned)

    return {"archived": archived, "pruned": pruned}
