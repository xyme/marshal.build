"""Composable agents R1 (spec: .kiro/specs/composable-agents, FSD §13.5R).

Composition is DECLARED in the requirements document with deterministic
phrases (the B13/B19 signal family), resolved and validated at every spec
write seam (save, rollback, import, generation), and stored on the project
row with RESOLVED project ids — slugs embed the name and change on rename,
so the id is the durable edge and the slug is display/provenance.

Graph rules (R1.3): no self-dependency, no cycles, depth ≤ 2 (A→B→C is the
deepest permitted chain). Validation failures surface as named 422s.
"""

import logging
import re
import uuid

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deployment, Project

logger = logging.getLogger("marshal.composition")

# "SHALL call agent <slug>" — single dependency; "SHALL orchestrate agents
# <a>, <b>[, ...]" — pipeline over the listed agents (each also a dependency).
_CALL_RE = re.compile(
    r"\bSHALL\s+call\s+agent\s+([A-Za-z0-9][A-Za-z0-9-]{1,60})\b", re.IGNORECASE
)
_ORCH_RE = re.compile(
    r"\bSHALL\s+orchestrate\s+agents\s+((?:[A-Za-z0-9][A-Za-z0-9-]{1,60})"
    r"(?:\s*,\s*[A-Za-z0-9][A-Za-z0-9-]{1,60})*)\b",
    re.IGNORECASE,
)
MAX_DEPENDENCIES = 5
MAX_DEPTH = 2
_HEX8_RE = re.compile(r"-([0-9a-f]{8})$")

AGENT_DEPENDENCY_SECRET_PREFIX = "marshal/agent-dependencies/"


class CompositionError(Exception):
    """Named composition refusal — surfaces as 422 {code, detail}."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def declared_dependencies(requirements_md: str) -> tuple[list[str], bool]:
    """(slugs, orchestrated) from the frozen text — deduped, ordered by first
    appearance (pipeline order is meaningful for orchestration)."""
    text = requirements_md or ""
    ordered: list[str] = []
    orchestrated = False
    for m in _ORCH_RE.finditer(text):
        orchestrated = True
        for raw in m.group(1).split(","):
            slug = raw.strip().lower()
            if slug and slug not in ordered:
                ordered.append(slug)
    for m in _CALL_RE.finditer(text):
        slug = m.group(1).lower()
        if slug not in ordered:
            ordered.append(slug)
    return ordered, orchestrated


async def _resolve_slug(db: AsyncSession, slug: str) -> Project | None:
    """Slug → project via the trailing 8-hex id fragment (export.project_slug
    shape: <name-slug>-<id[:8]>). Works across renames because only the id
    fragment is trusted; the name segment is display."""
    match = _HEX8_RE.search(slug)
    if not match:
        return None
    fragment = match.group(1)
    row = await db.execute(
        select(Project).where(cast(Project.id, String).like(f"{fragment}%"))
    )
    candidates = row.scalars().all()
    return candidates[0] if len(candidates) == 1 else None


async def _dependency_depth(db: AsyncSession, project_ids: list[uuid.UUID]) -> int:
    """Longest chain below the declared dependencies (their own compositions)."""
    depth = 1 if project_ids else 0
    frontier = list(project_ids)
    seen: set[uuid.UUID] = set(frontier)
    while frontier and depth <= MAX_DEPTH + 1:
        next_frontier: list[uuid.UUID] = []
        rows = (
            await db.execute(select(Project).where(Project.id.in_(frontier)))
        ).scalars().all()
        for proj in rows:
            for dep in ((proj.composition or {}).get("dependencies") or []):
                try:
                    dep_id = uuid.UUID(str(dep.get("project_id")))
                except (ValueError, TypeError):
                    continue
                if dep_id not in seen:
                    seen.add(dep_id)
                    next_frontier.append(dep_id)
        if next_frontier:
            depth += 1
        frontier = next_frontier
    return depth


async def _creates_cycle(
    db: AsyncSession, root_id: uuid.UUID, dep_ids: list[uuid.UUID]
) -> bool:
    """True when any declared dependency (transitively) reaches root."""
    frontier = list(dep_ids)
    seen: set[uuid.UUID] = set()
    while frontier:
        current = frontier.pop()
        if current == root_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        proj = await db.get(Project, current)
        for dep in (((proj.composition if proj else None) or {}).get("dependencies") or []):
            try:
                frontier.append(uuid.UUID(str(dep.get("project_id"))))
            except (ValueError, TypeError):
                continue
    return False


async def sync_project_composition(
    db: AsyncSession, project: Project, requirements_md: str
) -> dict | None:
    """Parse, resolve, validate and STORE composition on the project row.

    Called from every requirements write seam (save/rollback/import/
    generation) BEFORE commit. Returns the stored blob (None when the spec
    declares nothing — which also CLEARS a previously stored graph, keeping
    the row truthful to the latest content).
    """
    slugs, orchestrated = declared_dependencies(requirements_md)
    if not slugs:
        if project.composition is not None:
            project.composition = None
        return None
    if len(slugs) > MAX_DEPENDENCIES:
        raise CompositionError(
            "composition_limit",
            f"At most {MAX_DEPENDENCIES} agent dependencies are supported "
            f"(declared {len(slugs)})",
        )
    dependencies: list[dict] = []
    dep_ids: list[uuid.UUID] = []
    for slug in slugs:
        target = await _resolve_slug(db, slug)
        if target is None:
            raise CompositionError(
                "composition_unresolved",
                f"Declared agent '{slug}' does not resolve to a project — use "
                "the slug shown on the target project's page",
            )
        if target.id == project.id:
            raise CompositionError(
                "composition_self", "An agent cannot declare a dependency on itself"
            )
        dependencies.append(
            {"slug": slug, "project_id": str(target.id), "name": target.name}
        )
        dep_ids.append(target.id)
    if await _creates_cycle(db, project.id, dep_ids):
        raise CompositionError(
            "composition_cycle",
            "Declared dependencies form a cycle back to this agent",
        )
    depth = await _dependency_depth(db, dep_ids)
    if depth > MAX_DEPTH:
        raise CompositionError(
            "composition_depth",
            f"Dependency chains deeper than {MAX_DEPTH} levels are not "
            f"supported (this declaration reaches depth {depth})",
        )
    blob = {
        "dependencies": dependencies,
        "orchestration": "pipeline" if orchestrated else None,
        "depth": depth,
    }
    project.composition = blob
    return blob


def reach_score(composition: dict | None) -> tuple[int, str]:
    """composition_reach factor input (R4.1): deterministic 0-10 from the
    stored graph — computed, never model-scored."""
    deps = ((composition or {}).get("dependencies")) or []
    depth = int((composition or {}).get("depth") or (1 if deps else 0))
    orchestrated = bool((composition or {}).get("orchestration"))
    if not deps:
        return 0, "No declared agent dependencies."
    score = 3
    if len(deps) >= 3 or depth >= 2:
        score = 6
    if orchestrated and len(deps) >= 3:
        score = 8
    return score, (
        f"{len(deps)} declared agent dependenc{'y' if len(deps) == 1 else 'ies'}, "
        f"chain depth {depth}"
        + (", orchestrated pipeline" if orchestrated else "")
        + " (computed, not model-assessed)."
    )


async def dependents_with_active_deployments(
    db: AsyncSession, project_id: uuid.UUID
) -> list[str]:
    """Names of projects whose stored composition points at project_id AND
    that currently hold an active deployment (the R4.3 teardown guard)."""
    rows = (
        await db.execute(
            select(Project).where(Project.composition.is_not(None))
        )
    ).scalars().all()
    dependents = [
        p
        for p in rows
        if any(
            str(d.get("project_id")) == str(project_id)
            for d in ((p.composition or {}).get("dependencies") or [])
        )
    ]
    if not dependents:
        return []
    active = (
        await db.execute(
            select(Deployment.project_id).where(
                Deployment.project_id.in_([p.id for p in dependents]),
                Deployment.status == "active",
            )
        )
    ).scalars().all()
    active_set = set(active)
    return [p.name for p in dependents if p.id in active_set]
