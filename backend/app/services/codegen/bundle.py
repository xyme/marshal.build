"""Handoff bundle (codegen-handoff spec R6): spec snapshot + generated code +
build manifest in one zip. The `.kiro/specs/<slug>/` layout is the S4 export
contract; `generated/` + `build-manifest.json` are the S8 superset.
"""

import io
import json
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CodegenArtifact, CodegenBuild, Project
from app.services.export import project_slug


async def build_bundle_zip(
    db: AsyncSession, project: Project, build: CodegenBuild
) -> tuple[bytes, str]:
    """Returns (zip_bytes, filename). Ready builds only (enforced at the API).

    Layouts (cdk-artifacts R6): inline-cfn keeps the S8 shape (`generated/`);
    cdk-app is a working REPOSITORY — app files at the root, specs alongside,
    synth outputs under `synth/` — clone, `npm ci`, `npx cdk deploy`.
    """
    from app.services.codegen import storage

    artifacts = list(
        (
            await db.execute(
                select(CodegenArtifact).where(CodegenArtifact.build_id == build.id)
            )
        ).scalars()
    )
    slug = project_slug(project)
    repo_layout = build.artifact_profile == "cdk-app"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Spec docs exactly as the build snapshotted them (not current docs)
        for doc_type, doc in (build.spec_snapshot or {}).items():
            archive.writestr(f".kiro/specs/{slug}/{doc_type}.md", doc.get("content", ""))
        for artifact in artifacts:
            body = await storage.artifact_bytes(artifact)
            name = artifact.path if repo_layout else f"generated/{artifact.path}"
            archive.writestr(name, body)
        archive.writestr(
            "build-manifest.json",
            json.dumps(
                {
                    **(build.manifest or {}),
                    "build_id": str(build.id),
                    "content_hash": build.content_hash,
                    "project": {"id": str(project.id), "name": project.name, "slug": slug},
                    "layout": {"specs": f".kiro/specs/{slug}/", "code": "generated/"},
                },
                indent=2,
            ),
        )
    short = (build.content_hash or "build")[:8]
    return buffer.getvalue(), f"{slug}-build-{short}.zip"
