"""Template capability rail (agent-substance R3.2, task 5).

`guardrails.capabilities.allowed_capabilities` pins which ladder rungs specs
under a template may declare — an org template can hold its users to
inline/no-tools agents. ABSENT means all allowed (back-compat: every
existing template keeps working). Enforcement runs at every requirements
WRITE seam (editor save, rollback, import, generation — the composition
choke points) with a named 422, plus the deploy preflight (the rail may
tighten AFTER a build; the manifest is re-checked against the CURRENT rail
so a stale build cannot deploy past it).
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Template

logger = logging.getLogger("marshal.capabilities")

CAPABILITIES = ("memory", "packaged", "planning", "tools")
MAX_LOOP_ITERATIONS_CEILING = 8


class CapabilityNotAllowed(Exception):
    """Named 422 — the {code, detail} shape the composition refusals use."""

    code = "capability_not_allowed"

    def __init__(self, blocked: list[str], template_name: str):
        self.blocked = blocked
        self.detail = (
            f"Template '{template_name}' does not allow the declared "
            f"capabilit{'y' if len(blocked) == 1 else 'ies'}: "
            f"{', '.join(blocked)}. Remove the declaration(s) or switch "
            "templates."
        )
        super().__init__(self.detail)


def declared_capabilities(requirements_md: str) -> list[str]:
    """The ladder rungs a requirements document declares (deterministic —
    the same validate.py parsers every other consumer uses)."""
    from app.services.codegen.validate import (
        declared_tools,
        requires_memory,
        requires_packaged,
        requires_planning,
    )

    declared = []
    if requires_memory(requirements_md):
        declared.append("memory")
    if requires_packaged(requirements_md):
        declared.append("packaged")
    if requires_planning(requirements_md):
        declared.append("planning")
    if declared_tools(requirements_md):
        declared.append("tools")
    return declared


def allowed_capabilities(template: Template | None) -> set[str] | None:
    """The template's rail, or None when absent (= everything allowed)."""
    if template is None:
        return None
    rail = ((template.guardrails or {}).get("capabilities", {}) or {}).get(
        "allowed_capabilities"
    )
    if rail is None:
        return None
    return {str(c) for c in rail if str(c) in CAPABILITIES}


async def enforce_template_rail(
    db: AsyncSession, project: Project, requirements_md: str
) -> None:
    """Refuse requirements that declare rungs the project's template forbids
    (AC-4: a named 422 at spec save, not a deploy-time surprise)."""
    if not project.template_id:
        return
    template = await db.get(Template, project.template_id)
    allowed = allowed_capabilities(template)
    if allowed is None:
        return
    blocked = [c for c in declared_capabilities(requirements_md) if c not in allowed]
    if blocked:
        raise CapabilityNotAllowed(blocked, template.name if template else "unknown")


def blocked_for_manifest(manifest: dict, template: Template | None) -> list[str]:
    """Deploy-preflight twin: capabilities recorded on the BUILD manifest
    checked against the CURRENT template rail (rails may tighten post-build)."""
    allowed = allowed_capabilities(template)
    if allowed is None:
        return []
    declared = []
    if manifest.get("memory"):
        declared.append("memory")
    if manifest.get("packaged"):
        declared.append("packaged")
    if manifest.get("planning"):
        declared.append("planning")
    if manifest.get("tools"):
        declared.append("tools")
    return [c for c in declared if c not in allowed]


def capability_reach_score(requirements_md: str) -> tuple[int, str]:
    """Computed risk factor (agent-substance R3.1, rubric v3): declared
    ladder rungs → 0-10 score with a deterministic rationale. Never
    model-scored — governance visibility, like composition_reach."""
    declared = declared_capabilities(requirements_md)
    if not declared:
        return 0, "No capability rungs declared — a static generated agent."
    loops = "tools" in declared or "planning" in declared
    if loops and "packaged" in declared:
        score = 6
    elif loops:
        score = 5
    elif "packaged" in declared:
        score = 3
    else:  # memory alone
        score = 2
    if "memory" in declared and len(declared) > 1:
        score += 1
    return min(score, 8), (
        "Declared capability rungs: " + ", ".join(declared) + "."
    )


def validate_capabilities_blob(guardrails: dict | None) -> None:
    """Admin-surface validation for the capabilities rail — raises ValueError
    naming the offending field (mapped to TemplateLifecycleError upstream)."""
    blob = (guardrails or {}).get("capabilities")
    if blob is None:
        return
    if not isinstance(blob, dict):
        raise ValueError("guardrails.capabilities must be an object")
    rail = blob.get("allowed_capabilities")
    if rail is not None:
        if not isinstance(rail, list):
            raise ValueError(
                "guardrails.capabilities.allowed_capabilities must be a list"
            )
        unknown = [str(c) for c in rail if str(c) not in CAPABILITIES]
        if unknown:
            raise ValueError(
                "guardrails.capabilities.allowed_capabilities: unknown "
                f"capabilit{'y' if len(unknown) == 1 else 'ies'} "
                f"{', '.join(unknown)} (valid: {', '.join(CAPABILITIES)})"
            )
    cap = blob.get("max_loop_iterations")
    if cap is not None:
        if (
            not isinstance(cap, int)
            or isinstance(cap, bool)
            or not 1 <= cap <= MAX_LOOP_ITERATIONS_CEILING
        ):
            raise ValueError(
                "guardrails.capabilities.max_loop_iterations must be an "
                f"integer between 1 and {MAX_LOOP_ITERATIONS_CEILING}"
            )
