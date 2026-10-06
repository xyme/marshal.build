"""Admin audit viewer API (audit-logging spec R3/R4) — role-gated."""

import csv
import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.models import AuditLog, ModelInvocation, User
from app.schemas.audit import AuditDetailOut, AuditEntryOut, AuditListOut
from app.services import audit

logger = logging.getLogger("marshal.admin_audit")

router = APIRouter(
    prefix="/admin/audit-logs",
    tags=["admin-audit"],
    dependencies=[Depends(require_role("admin"))],
)

EXPORT_CAP = 10_000
AUDIT_CATEGORIES = {"user", "admin", "deployment", "security"}


def _period(
    from_: datetime | None, to: datetime | None
) -> tuple[datetime | None, datetime | None]:
    if from_ and from_.tzinfo is None:
        from_ = from_.replace(tzinfo=UTC)
    if to and to.tzinfo is None:
        to = to.replace(tzinfo=UTC) + timedelta(days=1)  # inclusive date-only "to"
    return from_, to


def _audit_query(
    q: str | None, user_id: uuid.UUID | None, category: str | None,
    from_: datetime | None, to: datetime | None,
) -> Select:
    from app.core.tenant import tenant_scope

    stmt = tenant_scope(select(AuditLog), AuditLog)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                AuditLog.action.ilike(like),
                AuditLog.resource_id.ilike(like),
                AuditLog.resource_type.ilike(like),
            )
        )
    if user_id:
        stmt = stmt.where(AuditLog.actor_id == user_id)
    if category:
        stmt = stmt.where(AuditLog.category == category)
    if from_:
        stmt = stmt.where(AuditLog.created_at >= from_)
    if to:
        stmt = stmt.where(AuditLog.created_at < to)
    return stmt


def _model_query(
    q: str | None, user_id: uuid.UUID | None,
    from_: datetime | None, to: datetime | None,
) -> Select:
    stmt = select(ModelInvocation)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(ModelInvocation.model_id.ilike(like), ModelInvocation.purpose.ilike(like))
        )
    if user_id:
        stmt = stmt.where(ModelInvocation.user_id == user_id)
    if from_:
        stmt = stmt.where(ModelInvocation.created_at >= from_)
    if to:
        stmt = stmt.where(ModelInvocation.created_at < to)
    return stmt


def _audit_out(row: AuditLog, emails: dict[uuid.UUID, str]) -> AuditEntryOut:
    return AuditEntryOut(
        id=row.id,
        created_at=row.created_at,
        source="audit",
        category=row.category,
        action=row.action,
        actor_id=row.actor_id,
        actor_email=emails.get(row.actor_id),
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        project_id=row.project_id,
        http_status=row.http_status,
    )


def _model_out(row: ModelInvocation, emails: dict[uuid.UUID, str]) -> AuditEntryOut:
    cost = f" · ${row.cost_usd:.4f}" if row.cost_usd is not None else ""
    return AuditEntryOut(
        id=row.id,
        created_at=row.created_at,
        source="model",
        category="model",
        action=f"bedrock_{row.purpose}",
        actor_id=row.user_id,
        actor_email=emails.get(row.user_id),
        resource_type="model",
        resource_id=row.model_id,
        project_id=row.project_id,
        summary=f"{row.model_id} · {row.input_tokens}→{row.output_tokens} tok{cost}"
        + ("" if row.success else " · FAILED"),
    )


async def _emails_for(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    ids.discard(None)
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.email).where(User.id.in_(ids)))
    return dict(result.all())


@router.get("", response_model=AuditListOut)
async def list_audit_logs(
    request: Request,
    q: str | None = None,
    user_id: uuid.UUID | None = None,
    category: str | None = Query(None, pattern="^(user|admin|deployment|security|model)$"),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AuditListOut:
    from_, to = _period(from_, to)
    window = page * page_size
    rows: list[tuple[datetime, str, object]] = []
    total = 0

    include_audit = category in (None, *AUDIT_CATEGORIES)
    include_model = category in (None, "model")

    if include_audit:
        stmt = _audit_query(q, user_id, category, from_, to)
        total += (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        result = await db.execute(
            stmt.order_by(AuditLog.created_at.desc()).limit(window)
        )
        rows += [(r.created_at, "audit", r) for r in result.scalars()]
    if include_model:
        stmt = _model_query(q, user_id, from_, to)
        total += (
            await db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        result = await db.execute(
            stmt.order_by(ModelInvocation.created_at.desc()).limit(window)
        )
        rows += [(r.created_at, "model", r) for r in result.scalars()]

    rows.sort(key=lambda t: t[0], reverse=True)
    page_rows = rows[(page - 1) * page_size : page * page_size]
    emails = await _emails_for(
        db, {getattr(r, "actor_id", None) or getattr(r, "user_id", None) for _, _, r in page_rows}
    )
    items = [
        _audit_out(r, emails) if kind == "audit" else _model_out(r, emails)
        for _, kind, r in page_rows
    ]
    # Viewer access is itself audit-worthy (R3.5).
    audit.emit(
        audit.write_entry(
            actor_id=admin.id,
            category=audit.ADMIN,
            action="audit_viewed",
            detail={"q": q, "category": category, "page": page},
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            http_status=200,
        )
    )
    return AuditListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{entry_id}", response_model=AuditDetailOut)
async def get_audit_entry(
    entry_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> AuditDetailOut:
    row = await db.get(AuditLog, entry_id)
    if row is not None:
        emails = await _emails_for(db, {row.actor_id})
        out = AuditDetailOut(**_audit_out(row, emails).model_dump())
        out.detail = row.detail or {}
        return out
    inv = await db.get(ModelInvocation, entry_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    emails = await _emails_for(db, {inv.user_id})
    out = AuditDetailOut(**_model_out(inv, emails).model_dump())
    out.detail = {
        "purpose": inv.purpose,
        "model_id": inv.model_id,
        "prompt_text": inv.prompt_text,
        "prompt_sha256": inv.prompt_sha256,
        "response_text": inv.response_text,
        "response_sha256": inv.response_sha256,
        "input_tokens": inv.input_tokens,
        "output_tokens": inv.output_tokens,
        "stop_reason": inv.stop_reason,
        "latency_ms": inv.latency_ms,
        "cost_usd": float(inv.cost_usd) if inv.cost_usd is not None else None,
        "success": inv.success,
        "error_class": inv.error_class,
        "session_id": str(inv.session_id) if inv.session_id else None,
        "generation_id": str(inv.generation_id) if inv.generation_id else None,
    }
    return out


@router.post("/export")
async def export_audit_logs(
    q: str | None = None,
    user_id: uuid.UUID | None = None,
    category: str | None = Query(None, pattern="^(user|admin|deployment|security|model)$"),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    fmt: str = Query("csv", alias="format", pattern="^(csv|ndjson)$"),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Export of the current filter result, capped at 10k rows (R3.4).

    `format=csv` (default) for humans/spreadsheets, `format=ndjson` for
    compliance handovers and machine ingestion (S14-06).
    """
    from_, to = _period(from_, to)
    entries: list[AuditEntryOut | AuditDetailOut] = []

    if category in (None, *AUDIT_CATEGORIES):
        result = await db.execute(
            _audit_query(q, user_id, category, from_, to)
            .order_by(AuditLog.created_at.desc())
            .limit(EXPORT_CAP)
        )
        for r in result.scalars():
            out = AuditDetailOut(**_audit_out(r, {}).model_dump())
            out.detail = r.detail or {}
            entries.append(out)
    if category in (None, "model") and len(entries) < EXPORT_CAP:
        result = await db.execute(
            _model_query(q, user_id, from_, to)
            .order_by(ModelInvocation.created_at.desc())
            .limit(EXPORT_CAP - len(entries))
        )
        for r in result.scalars():
            out = AuditDetailOut(**_model_out(r, {}).model_dump())
            out.detail = {
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
                "prompt_sha256": r.prompt_sha256,
                "success": r.success,
            }
            entries.append(out)
    entries.sort(key=lambda e: e.created_at, reverse=True)
    truncated = len(entries) >= EXPORT_CAP

    def rows():
        buf = io.StringIO()
        writer = csv.writer(buf)
        header = [
            "created_at", "source", "category", "action", "actor_id", "resource_type",
            "resource_id", "project_id", "http_status", "summary", "detail_json",
        ]
        if truncated:
            writer.writerow([f"# truncated to first {EXPORT_CAP} rows"])
        writer.writerow(header)
        for e in entries:
            writer.writerow([
                e.created_at.isoformat(), e.source, e.category, e.action,
                e.actor_id or "", e.resource_type or "", e.resource_id or "",
                e.project_id or "", e.http_status or "", e.summary or "",
                json.dumps(e.detail, default=str),
            ])
            if buf.tell() > 64_000:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        yield buf.getvalue()

    def ndjson_rows():
        """S14-06: machine-readable handover for compliance requests. Same
        filters and same cap as the CSV — an export must never disagree with
        what the reviewer saw on screen."""
        if truncated:
            yield json.dumps({"_truncated_to": EXPORT_CAP}) + "\n"
        for e in entries:
            yield json.dumps(
                {
                    "created_at": e.created_at.isoformat(),
                    "source": e.source,
                    "category": e.category,
                    "action": e.action,
                    "actor_id": str(e.actor_id) if e.actor_id else None,
                    "resource_type": e.resource_type,
                    "resource_id": e.resource_id,
                    "project_id": str(e.project_id) if e.project_id else None,
                    "http_status": e.http_status,
                    "summary": e.summary,
                    "detail": e.detail,
                },
                default=str,
            ) + "\n"

    stamp = f"{datetime.now(UTC):%Y%m%d-%H%M%S}"
    if fmt == "ndjson":
        return StreamingResponse(
            ndjson_rows(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="marshal-audit-{stamp}.jsonl"'
            },
        )
    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="marshal-audit-{stamp}.csv"'
        },
    )
