"""Tenant resolution + the query seam (platform-scale-policies spec R4).

Beta runs a single platform tenant; resolution reads a JWT claim stub
(`custom:tenant`) and falls back to the platform tenant. The seam exists —
and is exercised by cross-tenant isolation tests — so GA multi-tenancy is
an activation exercise (map real claims, add tenant admin), not a rewrite.
"""

import uuid

from fastapi import Request

from app.models.entities import PLATFORM_TENANT_ID

__all__ = ["PLATFORM_TENANT_ID", "current_tenant_id", "tenant_scope", "assert_tenant"]


def current_tenant_id(request: Request | None = None) -> uuid.UUID:
    """Claim stub → tenant id. Absent/unknown → the platform tenant."""
    if request is not None:
        claims = getattr(request.state, "token_claims", None) or {}
        raw = claims.get("custom:tenant")
        if raw:
            try:
                return uuid.UUID(str(raw))
            except ValueError:
                pass
    return PLATFORM_TENANT_ID


def tenant_scope(stmt, model, tenant_id: uuid.UUID | None = None):
    """Append the tenant filter to a select over a TenantMixin model."""
    return stmt.where(model.tenant_id == (tenant_id or PLATFORM_TENANT_ID))


def assert_tenant(obj, tenant_id: uuid.UUID | None = None) -> bool:
    """Post-load guard for direct id fetches."""
    return getattr(obj, "tenant_id", None) == (tenant_id or PLATFORM_TENANT_ID)
