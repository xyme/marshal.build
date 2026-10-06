"""B9 service-account admin surface (integration-wave spec R2)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.db import get_db
from app.models import User
from app.services import audit
from app.services import service_accounts as svc

router = APIRouter(
    prefix="/admin/service-accounts",
    tags=["admin-service-accounts"],
    dependencies=[Depends(require_role("admin"))],
)


class AccountIn(BaseModel):
    name: str = Field(min_length=3, max_length=60)
    role: str = Field(pattern="^(business|power)$")  # never admin (product decision)


class TokenIn(BaseModel):
    name: str = Field(default="token", max_length=80)
    expires_in_days: int = Field(default=90, ge=1, le=365)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(active|suspended)$")


@router.get("")
async def list_service_accounts(db: AsyncSession = Depends(get_db)) -> dict:
    return {"items": await svc.list_accounts(db)}


@router.post("", status_code=201)
async def create_service_account(
    payload: AccountIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        account = await svc.create_account(
            db, name=payload.name, role=payload.role, actor=admin
        )
    except svc.ServiceAccountError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, name=account.name, role=account.role)
    return {"id": str(account.id), "name": account.name, "role": account.role}


@router.put("/{account_id}/status")
async def set_service_account_status(
    account_id: uuid.UUID,
    payload: StatusIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    account = await db.get(User, account_id)
    if account is None or account.kind != "service":
        raise HTTPException(status_code=404, detail="Service account not found")
    account.status = payload.status
    await db.commit()
    audit.set_audit_detail(request, name=account.name, status=payload.status)
    return {"id": str(account.id), "status": account.status}


@router.post("/{account_id}/tokens", status_code=201)
async def mint_service_token(
    account_id: uuid.UUID,
    payload: TokenIn,
    request: Request,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    account = await db.get(User, account_id)
    if account is None or account.kind != "service":
        raise HTTPException(status_code=404, detail="Service account not found")
    try:
        result = await svc.mint_token(
            db, account,
            name=payload.name, expires_in_days=payload.expires_in_days, actor=admin,
        )
    except svc.ServiceAccountError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # The VALUE is in the response body once and never in the audit trail.
    audit.set_audit_detail(
        request, account=account.name, token_id=result["id"],
        expires_at=result["expires_at"],
    )
    return result


@router.delete("/{account_id}/tokens/{token_id}")
async def revoke_service_token(
    account_id: uuid.UUID,
    token_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    account = await db.get(User, account_id)
    if account is None or account.kind != "service":
        raise HTTPException(status_code=404, detail="Service account not found")
    try:
        await svc.revoke_token(db, account, token_id)
    except svc.ServiceAccountError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit.set_audit_detail(request, account=account.name, token_id=str(token_id))
    return {"revoked": True}
