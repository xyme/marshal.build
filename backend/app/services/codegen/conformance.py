"""Spec-conformance report + deterministic gate inputs (FSD B7/B23).

Two passes inspect whether generated code honors its frozen requirements:

1. Deterministic contract extraction — positive normative quoted enumeration
   literals and ``{field, …}`` brace lists are verified textually. The same
   machine-readable contract is injected into every generation stage (B23).
2. Temperature-0 model review covers richer criteria and stays ADVISORY.

This module itself never changes build state: it degrades to partial results
or ``status: unavailable``. The shared runner finalizer promotes ONLY
``source=deterministic`` violated verdicts to blocking findings; model review
can be wrong and never gates.
"""

import json
import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CodegenBuild, Project, Template
from app.services.bedrock import InvocationCtx, converse
from app.services.codegen.literals import Contract, extract_contracts
from app.services.guardrails import resolve_models

logger = logging.getLogger("marshal.codegen")

# Model-review limits stay here; deterministic extraction lives in the
# stdlib-only literals module so the minimal external runner imports no DB stack.
MAX_REVIEW_CRITERIA = 12
_CRITERION_CLIP = 240
SOURCE_SUFFIXES = (".py", ".ts")


def _clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[:budget] + "\n[...clipped...]"


def _source_corpus(artifacts: dict[str, str]) -> str:
    return "\n".join(
        content for path, content in sorted(artifacts.items())
        if path.endswith(SOURCE_SUFFIXES)
    )


def deterministic_verdicts(
    contracts: list[Contract], artifacts: dict[str, str]
) -> list[dict]:
    """Pure. Substring presence of every contract item in the source corpus.

    Raw-text matching only (R1.2): a missing enum literal or field name is a
    `violated` verdict citing the criterion line; a fully-present contract is
    one `met` verdict. No AST, no guessing.
    """
    corpus = _source_corpus(artifacts)
    verdicts: list[dict] = []
    for contract in contracts:
        missing = [item for item in contract.items if item not in corpus]
        label = "enumeration value(s)" if contract.kind == "enum_literals" else "field(s)"
        if missing:
            verdicts.append(
                {
                    "source": "deterministic",
                    "check": contract.kind,
                    "verdict": "violated",
                    "criterion": contract.criterion,
                    "evidence": (
                        f"{label} {', '.join(repr(m) for m in missing)} from this "
                        "criterion never appear in the generated source"
                    ),
                }
            )
        else:
            verdicts.append(
                {
                    "source": "deterministic",
                    "check": contract.kind,
                    "verdict": "met",
                    "criterion": contract.criterion,
                    "evidence": (
                        f"all {len(contract.items)} {label} appear in the generated "
                        f"source ({', '.join(contract.items)})"
                    ),
                }
            )
    return verdicts


REVIEW_SYSTEM = """You are the spec-conformance reviewer for marshal. You receive an \
agent's requirements document and the SOURCE FILES that were generated from it. \
Judge whether the generated code honors the specification's acceptance criteria.

Rules:
- Base every verdict ONLY on the source text provided. Do not assume unshown code.
- verdict "met": the source demonstrably satisfies the criterion — quote the evidence.
- verdict "violated": the source contradicts the criterion (wrong/renamed enumeration \
values, invented or missing fields, missing endpoints, dropped requirements) — quote \
the offending source fragment.
- verdict "unverifiable": the criterion cannot be judged from source text alone \
(runtime behavior, operations, latency) — say why in one clause.
- Prefer the document's own acceptance-criteria wording; keep each criterion under \
200 characters. At most 12 criteria: pick the most substantive.
- Pay particular attention to: enumeration/band/status values (exact names), input \
field names (invented fields count as violations when the spec closes the set), \
API routes and methods, and explicit SHALL/SHALL NOT clauses.

Reply with ONLY this JSON (no fences, no prose):
{"criteria": [{"criterion": "...", "verdict": "met|violated|unverifiable", "evidence": "..."}]}"""


def _parse_review_json(text: str) -> list[dict]:
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fence:
        candidate = fence.group(1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in review response")
    parsed = json.loads(candidate[start : end + 1])
    entries = parsed.get("criteria")
    if not isinstance(entries, list) or not entries:
        raise ValueError("review JSON has no criteria list")
    out: list[dict] = []
    for entry in entries[:MAX_REVIEW_CRITERIA]:
        verdict = str(entry.get("verdict", "")).strip().lower()
        if verdict not in ("met", "violated", "unverifiable"):
            raise ValueError(f"bad verdict {verdict!r}")
        out.append(
            {
                "source": "review",
                "verdict": verdict,
                "criterion": str(entry.get("criterion", ""))[:_CRITERION_CLIP],
                "evidence": str(entry.get("evidence", ""))[:_CRITERION_CLIP],
            }
        )
    return out


async def review_criteria(
    requirements_md: str,
    artifacts: dict[str, str],
    *,
    model_id: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID,
    build_id: uuid.UUID,
) -> list[dict]:
    """Temp-0 fast-model criteria review (R1.3). Raises on unusable output
    after one retry — the ORCHESTRATOR owns degradation, not this function."""
    sources = "\n\n".join(
        f"=== {path} ===\n{_clip(content, 6000)}"
        for path, content in sorted(artifacts.items())
        if path.endswith(SOURCE_SUFFIXES)
    )
    if not sources:
        raise ValueError("no source files to review")
    prompt = (
        f"REQUIREMENTS DOCUMENT:\n{_clip(requirements_md, 12000)}\n\n"
        f"GENERATED SOURCE FILES:\n{_clip(sources, 16000)}\n\n"
        "Review conformance now."
    )
    ctx = InvocationCtx(
        purpose="classification",  # governance purpose: cap-exempt, recorded (R1.3)
        user_id=user_id,
        project_id=project_id,
        generation_id=build_id,
    )
    last_error: Exception | None = None
    for _attempt in range(2):  # one retry on malformed output (risk.py precedent)
        text, _usage, _stop = await converse(
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=REVIEW_SYSTEM,
            model_id=model_id,
            max_tokens=1500,
            temperature=0.0,
            ctx=ctx,
        )
        try:
            return _parse_review_json(text)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("conformance review parse failed (attempt): %s", exc)
    raise ValueError(f"conformance review unparseable after retry: {last_error}")


def _summarize(verdicts: list[dict]) -> dict:
    summary = {"met": 0, "violated": 0, "unverifiable": 0}
    for v in verdicts:
        summary[v["verdict"]] = summary.get(v["verdict"], 0) + 1
    return summary


async def conformance_report(
    db: AsyncSession,
    build: CodegenBuild,
    artifacts: dict[str, str],
    *,
    contracts: list[Contract] | None = None,
) -> dict:
    """Orchestrator: deterministic pass then advisory model review. Partial
    results beat nothing; NEVER raises. `contracts` avoids re-parsing the
    frozen requirements after BuildCtx has already normalized them."""
    try:
        requirements = str(
            ((build.spec_snapshot or {}).get("requirements") or {}).get("content", "")
        )
        if not requirements.strip():
            return {"status": "unavailable", "error": "no requirements snapshot on the build"}

        effective_contracts = contracts if contracts is not None else extract_contracts(requirements)
        det = deterministic_verdicts(effective_contracts, artifacts)

        model_id: str | None = None
        review: list[dict] = []
        review_error: str | None = None
        try:
            project = await db.get(Project, build.project_id)
            template = (
                await db.get(Template, project.template_id)
                if project is not None and project.template_id
                else None
            )
            models = await resolve_models(template)
            model_id = models.fast
            review = await review_criteria(
                requirements,
                artifacts,
                model_id=model_id,
                user_id=build.created_by,
                project_id=build.project_id,
                build_id=build.id,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never block (R1.4)
            review_error = str(exc)[:300]
            logger.warning("conformance model review unavailable for build %s: %s", build.id, exc)

        verdicts = det + review
        if not verdicts:
            return {
                "status": "unavailable",
                "error": review_error or "no checkable criteria found",
            }
        report: dict = {
            "status": "ok",
            "model_id": model_id if review else None,
            "summary": _summarize(verdicts),
            "verdicts": verdicts,
        }
        if review_error:
            report["review_error"] = review_error
        return report
    except Exception as exc:  # noqa: BLE001 — the R1.4 outer belt
        logger.exception("conformance report crashed for build %s", build.id)
        return {"status": "unavailable", "error": str(exc)[:300]}
