"""Shared API dependencies — the project access seam (collaboration spec R1).

Every project-scoped endpoint declares its minimum role through
`require_project_role`; non-members (admins included, outside the governance
carve-out documented in services/collab.py) get 404 — membership is not
disclosed (R1.2).
"""

import uuid

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin_security_if_admin
from app.core.db import get_db
from app.models import Project, User
from app.services import collab


def require_project_role(min_role: str):
    """Dependency factory: resolve (project, role), enforce role ≥ min_role.

    The resolved role is stashed on request.state.project_role for endpoints
    that surface it (e.g. ProjectOut.my_role).
    """

    async def checker(
        project_id: uuid.UUID,
        request: Request,
        user: User = Depends(require_admin_security_if_admin),
        db: AsyncSession = Depends(get_db),
    ) -> Project:
        resolved = await collab.resolve_role(db, project_id, user)
        if resolved is None:
            raise HTTPException(status_code=404, detail="Project not found")
        project, role = resolved
        if not collab.role_atleast(role, min_role):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action needs {min_role} access on the project; you have "
                    f"{role}. The project owner can change this under Share, or by "
                    "filing the project into a team where you are a lead."
                ),
            )
        request.state.project_role = role
        return project

    return checker


# Prebuilt levels per the R1.3 capability matrix
project_viewer = require_project_role("viewer")
project_editor = require_project_role("editor")
project_owner = require_project_role("owner")
