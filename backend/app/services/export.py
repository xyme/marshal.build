"""Spec export as a KIRO-compatible zip (alpha-polish spec R1, S4-06).

The zip layout IS the future S8 handoff contract: unpacking into a repository
yields a valid `.kiro/specs/<slug>/` directory.
"""

import io
import json
import re
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Spec, Template

DOC_ORDER = ("requirements", "design", "tasks")


class NothingToExport(Exception):
    """Project has no saved documents — surfaces as 409."""


def project_slug(project: Project) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", project.name.lower()).strip("-") or "project"
    return f"{slug[:48]}-{str(project.id)[:8]}"  # id suffix keeps slugs collision-free


async def build_export_zip(db: AsyncSession, project: Project) -> tuple[bytes, str, dict]:
    """Returns (zip_bytes, filename, manifest). Docs export byte-identical."""
    docs: dict[str, Spec] = {}
    for doc_type in DOC_ORDER:
        result = await db.execute(
            select(Spec)
            .where(Spec.project_id == project.id, Spec.type == doc_type)
            .order_by(Spec.version.desc())
            .limit(1)
        )
        spec = result.scalar_one_or_none()
        if spec:
            docs[doc_type] = spec
    if not docs:
        raise NothingToExport(project.name)

    template = await db.get(Template, project.template_id) if project.template_id else None
    from app.services import risk as risk_svc

    assessment = await risk_svc.latest_assessment(db, project.id)

    slug = project_slug(project)
    manifest = {
        "project": {"id": str(project.id), "name": project.name, "slug": slug},
        "documents": {
            doc_type: (
                {"version": docs[doc_type].version, "origin": docs[doc_type].origin}
                if doc_type in docs
                else {"absent": True}
            )
            for doc_type in DOC_ORDER
        },
        "template": (
            {"name": template.name, "version": template.version} if template else None
        ),
        "risk": (
            {"score": assessment.score, "level": assessment.level, "decision": assessment.decision}
            if assessment and assessment.status == "scored"
            else None
        ),
        "exported_at": datetime.now(UTC).isoformat(),
        "platform": "marshal Alpha",
        "layout": f".kiro/specs/{slug}/",
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for doc_type, spec in docs.items():
            archive.writestr(f".kiro/specs/{slug}/{doc_type}.md", spec.content)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))

    max_version = max(s.version for s in docs.values())
    filename = f"{slug}-spec-v{max_version}.zip"
    return buffer.getvalue(), filename, manifest
