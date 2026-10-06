"""Codegen build endpoints (codegen-handoff spec R2/R4/R6/R8).

Mutations are editor+ (collaboration seam); reads are member-visible.
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import project_editor, project_viewer
from app.core.auth import get_current_user, require_admin_security_if_admin
from app.core.db import get_db
from app.models import CodegenArtifact, CodegenBuild, Project, User
from app.services import analytics, audit
from app.services.codegen import CodegenUnavailable, runner
from app.services.codegen import bundle as bundle_svc

router = APIRouter(tags=["builds"])


def _build_out(build: CodegenBuild, *, creator_name: str | None = None) -> dict:
    return {
        "id": str(build.id),
        "project_id": str(build.project_id),
        "status": build.status,
        "provider": build.provider,
        "external_job_id": build.external_job_id,
        "retried": build.retried,
        "artifact_profile": build.artifact_profile,
        "phase_detail": build.phase_detail,
        "spec_hash": build.spec_hash,
        "content_hash": build.content_hash,
        "manifest": build.manifest or {},
        "error": build.error,
        "created_by_name": creator_name,
        "spec_versions": {
            t: d.get("version") for t, d in (build.spec_snapshot or {}).items()
        },
        "created_at": build.created_at.isoformat(),
        "started_at": build.started_at.isoformat() if build.started_at else None,
        "finished_at": build.finished_at.isoformat() if build.finished_at else None,
    }


async def _names(db: AsyncSession, builds: list[CodegenBuild]) -> dict:
    ids = {b.created_by for b in builds if b.created_by}
    if not ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(ids)))
    return {u.id: (u.name or u.email.split("@")[0]) for u in rows.scalars()}


class BuildCreateIn(BaseModel):
    artifact_profile: str | None = None  # inline-cfn | cdk-app (S10 R1.1)


@router.post("/projects/{project_id}/builds", status_code=202)
async def create_build(
    request: Request,
    payload: BuildCreateIn | None = None,
    project: Project = Depends(project_editor),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        build = await runner.start_build(
            db, user, project,
            artifact_profile=payload.artifact_profile if payload else None,
        )
    except runner.BuildInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except runner.NothingToBuild as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except runner.ProfileUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CodegenUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    audit.set_audit_detail(
        request, resource_id=str(build.id), spec_hash=build.spec_hash,
        artifact_profile=build.artifact_profile,
    )
    analytics.track(user.id, "build_started", project_id=project.id)  # S16-04
    return _build_out(build)


@router.get("/projects/{project_id}/builds")
async def list_builds(
    project: Project = Depends(project_viewer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = list(
        (
            await db.execute(
                select(CodegenBuild)
                .where(CodegenBuild.project_id == project.id)
                .order_by(CodegenBuild.created_at.desc())
                .limit(30)
            )
        ).scalars()
    )
    names = await _names(db, rows)
    return {"items": [_build_out(b, creator_name=names.get(b.created_by)) for b in rows]}


async def _accessible_build(
    build_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> CodegenBuild:
    build = await db.get(CodegenBuild, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="Build not found")
    from app.services import collab

    resolved = await collab.resolve_role(db, build.project_id, user)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Build not found")
    request.state.project_role = resolved[1]
    return build


@router.get("/builds/{build_id}")
async def get_build(
    build: CodegenBuild = Depends(_accessible_build),
    db: AsyncSession = Depends(get_db),
) -> dict:
    names = await _names(db, [build])
    return _build_out(build, creator_name=names.get(build.created_by))


@router.get("/builds/{build_id}/events")
async def stream_build_events(
    build: CodegenBuild = Depends(_accessible_build),
) -> EventSourceResponse:
    snapshot = {
        "status": build.status,
        "phase_detail": build.phase_detail,
        "error": build.error,
    }
    is_terminal = build.status in runner.TERMINAL_STATES
    build_id = str(build.id)

    async def event_stream():
        yield {"event": "snapshot", "data": json.dumps(snapshot)}
        if is_terminal:
            yield {"event": "done", "data": json.dumps({"status": snapshot["status"]})}
            return
        queue = await runner.codegen_bus.subscribe(build_id)
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "done":
                    yield {"event": "done", "data": json.dumps(event)}
                    return
                yield {"event": event.get("type", "update"), "data": json.dumps(event)}
        finally:
            await runner.codegen_bus.unsubscribe(build_id, queue)

    return EventSourceResponse(event_stream())


@router.get("/builds/{build_id}/artifacts")
async def list_artifacts(
    build: CodegenBuild = Depends(_accessible_build),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.execute(
        select(
            CodegenArtifact.path,
            CodegenArtifact.content_hash,
            CodegenArtifact.size_bytes,
            CodegenArtifact.language,
        )
        .where(CodegenArtifact.build_id == build.id)
        .order_by(CodegenArtifact.path.asc())
    )
    return {
        "items": [
            {"path": p, "content_hash": h, "size_bytes": s, "language": lang}
            for p, h, s, lang in rows.all()
        ]
    }


@router.get("/builds/{build_id}/artifacts/{artifact_path:path}")
async def get_artifact(
    artifact_path: str,
    download: bool = False,
    build: CodegenBuild = Depends(_accessible_build),
    db: AsyncSession = Depends(get_db),
):
    from app.services.codegen import storage

    artifact = (
        await db.execute(
            select(CodegenArtifact).where(
                CodegenArtifact.build_id == build.id, CodegenArtifact.path == artifact_path
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    binary = storage.is_binary_path(artifact.path) or artifact.language == "binary"
    if download:
        body = await storage.artifact_bytes(artifact)
        filename = artifact.path.rsplit("/", 1)[-1]
        return Response(
            content=body,
            media_type="application/zip" if binary else "text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    if binary:
        return {
            "path": artifact.path, "content": None, "binary": True,
            "content_hash": artifact.content_hash, "size_bytes": artifact.size_bytes,
            "language": artifact.language,
        }
    if artifact.size_bytes > storage.PREVIEW_CAP_BYTES:
        return {
            "path": artifact.path, "content": None, "binary": False, "too_large": True,
            "content_hash": artifact.content_hash, "size_bytes": artifact.size_bytes,
            "language": artifact.language,
        }
    return {
        "path": artifact.path,
        "content": await storage.artifact_text(artifact),
        "binary": False,
        "content_hash": artifact.content_hash,
        "language": artifact.language,
    }


@router.post("/builds/{build_id}/cancel")
async def cancel_build(
    request: Request,
    build: CodegenBuild = Depends(_accessible_build),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    role = getattr(request.state, "project_role", "viewer")
    if build.created_by != user.id and role != "owner":
        raise HTTPException(status_code=403, detail="Only the person who started this build, or the project owner, can cancel it")
    if build.status in runner.TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="This build has already finished — start a new build to make changes")
    if build.external_job_id is not None or build.provider != "internal":
        # External builds: tombstone + StopBuild, terminal now (S9 R4.5)
        await runner.cancel_external(db, build)
    else:
        runner.request_cancel(build.id)
    audit.set_audit_detail(request, project_id=str(build.project_id))
    return {"id": str(build.id), "cancel_requested": True}


@router.get("/builds/{build_id}/bundle")
async def download_bundle(
    request: Request,
    build: CodegenBuild = Depends(_accessible_build),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if build.status != "ready":
        raise HTTPException(status_code=409, detail="The handoff bundle is available once the build reaches READY")
    from app.services.exportlimit import check_export_rate

    check_export_rate(request.state.user.id)
    project = await db.get(Project, build.project_id)
    payload, filename = await bundle_svc.build_bundle_zip(db, project, build)
    # GETs skip the audit middleware; bundle downloads are audit-worthy (R6.3)
    user = getattr(request.state, "user", None)
    audit.emit(
        audit.write_entry(
            actor_id=user.id if user else None,
            category=audit.USER,
            action="build_bundle_downloaded",
            resource_type="build",
            resource_id=str(build.id),
            project_id=build.project_id,
            detail={"filename": filename, "content_hash": build.content_hash},
            source_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            http_status=200,
        )
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
