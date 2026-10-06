"""AI risk scoring engine + deployment gate (FSD §4.6.3, S4-02/03).

Determinism (§4.6.9 AC-3): assessments are keyed by sha256 of the project's
three documents + rubric version. A hash hit returns the stored row — the LLM
is consulted exactly once per distinct content. Three factors come from a
temperature-0 fast-model call with a fixed rubric; model_capability and
deployment_scope are computed in code so scores stay anchored and cheap.
"""

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiRiskAssessment, Project, Spec, Template, User
from app.services.bedrock import InvocationCtx, converse
from app.services.guardrails import model_registry, resolve_models

logger = logging.getLogger("marshal.risk")

# RUBRIC_VERSION 3 (agent-substance R3.1, the deferred one-bump-for-all): the
# seventh computed factor `capability_reach` joined the rubric — declared
# ladder rungs (memory/packaged/tools/planning) are visible to scoring, so a
# tool-using agent scores differently from a static one under an org's
# policy. Every distinct content re-scores ONCE under the new rubric (the
# B17 determinism key includes this version precisely so factor changes are
# cache misses, never silent drift). v2 added composition_reach (13.5R).
RUBRIC_VERSION = 3

# FSD §4.6.3 weights — the PLATFORM DEFAULTS. B17: admins may tune weights,
# band thresholds, auto-approve and the LLM-factor anchor lines through the
# versioned risk policy (resolve_risk_policy); the five v1 factors ARE policy
# version 1; composition_reach arrived with rubric v2, capability_reach v3.
WEIGHTS = {
    "data_sensitivity": 0.30,
    "model_capability": 0.20,
    "user_facing": 0.20,
    "deployment_scope": 0.15,
    "request_volume": 0.15,
    "composition_reach": 0.10,
    "capability_reach": 0.10,
}

# Computed factors an admin tuning may omit (they auto-fill at their default
# SHARE — the 13.5R proportional-fill rule generalized to every rubric growth).
COMPUTED_OPTIONAL_FACTORS = ("composition_reach", "capability_reach")
LLM_FACTORS = ("data_sensitivity", "user_facing", "request_volume")
TIER_CAPABILITY = {"advanced": 8, "standard": 5, "fast": 2}
DEPLOYMENT_SCOPE_SANDBOX = 3  # Alpha deploys only into governed sandboxes

DEFAULT_BAND_LOW_MAX = 30
DEFAULT_BAND_MEDIUM_MAX = 60
MAX_ANCHOR_CHARS = 600

# Default anchor line per LLM-scored factor. Overridable per-factor by the
# risk policy (R1.4); assembled into the system prompt, never stored.
DEFAULT_ANCHORS = {
    "data_sensitivity": "0-1 no meaningful data; 3 generic internal documents; 6 personal data (names, emails); 8 regulated PII/financial/credit data; 10 health or government-identity data.",
    "user_facing": "0-2 internal tooling for a small team; 5 broad internal staff exposure; 8 external customers interact directly; 10 external customers in high-stakes decisions.",
    "request_volume": "2 low/ad-hoc usage; 5 steady departmental usage; 7 organization-wide or thousands of requests/day; 10 public internet scale.",
}

_RUBRIC_TEMPLATE = """You are a risk assessor for an enterprise AI application platform.
Score the application described by the specification documents on exactly these factors, each an integer 0-10:

- data_sensitivity: {data_sensitivity}
- user_facing: {user_facing}
- request_volume: {request_volume}

Respond with ONLY a JSON object, no prose, exactly:
{{"data_sensitivity": {{"score": N, "rationale": "one sentence"}},
 "user_facing": {{"score": N, "rationale": "one sentence"}},
 "request_volume": {{"score": N, "rationale": "one sentence"}}}}"""

DOC_ORDER = ("requirements", "design", "tasks")

# Per-project in-flight locks: debounce concurrent triggers (R2.1)
_inflight: dict[uuid.UUID, asyncio.Lock] = {}


class RiskDecisionError(Exception):
    """Invalid decision transition — surfaces as 422."""


class ScoringInProgress(Exception):
    """Another scorer holds the lock — deploy surfaces 409 retryable."""


class RiskPolicyError(Exception):
    """Invalid policy payload — surfaces as 422 naming the field."""


@dataclass(frozen=True)
class RiskPolicy:
    """Effective admin risk policy (B17). version 1 == the code defaults."""

    version: int = 1
    auto_approve_low: bool = True
    band_low_max: int = DEFAULT_BAND_LOW_MAX
    band_medium_max: int = DEFAULT_BAND_MEDIUM_MAX
    weights: dict = field(default_factory=lambda: dict(WEIGHTS))
    anchors: dict = field(default_factory=dict)  # overrides only, LLM factors

    def normalized_weights(self) -> dict:
        total = sum(self.weights.values())
        return {k: v / total for k, v in self.weights.items()}

    def anchor(self, factor: str) -> str:
        return self.anchors.get(factor) or DEFAULT_ANCHORS[factor]


def validate_policy_payload(payload: dict) -> None:
    """Hard validation for PUT payloads — raise RiskPolicyError naming the
    offending field (R3.1). Partial payloads allowed; only present keys
    are checked."""
    if "auto_approve_low" in payload and not isinstance(payload["auto_approve_low"], bool):
        raise RiskPolicyError("auto_approve_low must be a boolean")
    for key in ("band_low_max", "band_medium_max"):
        v = payload.get(key)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 99):
            raise RiskPolicyError(f"{key} must be an integer between 1 and 99")
    weights = payload.get("weights")
    if weights is not None:
        # Rubric v2/v3 transitions: a tuning naming the five v1 factors stays
        # valid with ANY subset of the computed factors — the missing ones
        # auto-fill at their default share. A stored admin tuning must never
        # be refused OR silently reset by rubric growth (13.5R rule).
        required = set(WEIGHTS) - set(COMPUTED_OPTIONAL_FACTORS)
        valid_shape = (
            isinstance(weights, dict)
            and required <= set(weights) <= set(WEIGHTS)
        )
        if not valid_shape:
            raise RiskPolicyError(
                "weights must name the rubric factors: "
                + ", ".join(sorted(WEIGHTS))
                + " ("
                + ", ".join(COMPUTED_OPTIONAL_FACTORS)
                + " may be omitted — they default to their platform shares)"
            )
        for name, w in weights.items():
            if not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
                raise RiskPolicyError(f"weights.{name} must be a number greater than zero")
    anchors = payload.get("anchors")
    if anchors is not None:
        if not isinstance(anchors, dict):
            raise RiskPolicyError("anchors must be an object")
        for name, text in anchors.items():
            if name not in LLM_FACTORS:
                raise RiskPolicyError(
                    f"anchors.{name}: only model-scored factors accept overrides "
                    f"({', '.join(LLM_FACTORS)})"
                )
            if not isinstance(text, str) or not text.strip():
                raise RiskPolicyError(f"anchors.{name} must be a non-empty string")
            if len(text) > MAX_ANCHOR_CHARS:
                raise RiskPolicyError(
                    f"anchors.{name} exceeds {MAX_ANCHOR_CHARS} characters"
                )


def _policy_from_blob(blob: dict) -> RiskPolicy:
    blob = blob or {}
    low = int(blob.get("band_low_max", DEFAULT_BAND_LOW_MAX))
    medium = int(blob.get("band_medium_max", DEFAULT_BAND_MEDIUM_MAX))
    if not 1 <= low < medium <= 99:  # stored trouble degrades to defaults
        low, medium = DEFAULT_BAND_LOW_MAX, DEFAULT_BAND_MEDIUM_MAX
    provided = {
        k: v
        for k, v in (blob.get("weights") or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    }
    weights = dict(provided)
    # Tolerant fill (rubric v2): a stored five-factor tuning keeps its values
    # and gains the new factor at its DEFAULT SHARE — never a wholesale reset
    # (that would silently discard admin governance), and never an absolute
    # fill (B17 weights are scale-free proportions: an admin's 30/20/15 scale
    # must not dilute the new factor to 0.1 effective parts per hundred).
    missing = [k for k in WEIGHTS if k not in weights]
    if weights and missing:
        provided_total = sum(weights.values())
        base_total = sum(WEIGHTS[k] for k in weights if k in WEIGHTS) or 1.0
        for k in missing:
            weights[k] = WEIGHTS[k] * provided_total / base_total
    if set(weights) != set(WEIGHTS) or any(
        not isinstance(w, (int, float)) or w <= 0 for w in weights.values()
    ):
        weights = dict(WEIGHTS)
    anchors = {
        k: str(v)[:MAX_ANCHOR_CHARS]
        for k, v in (blob.get("anchors") or {}).items()
        if k in LLM_FACTORS and str(v).strip()
    }
    return RiskPolicy(
        version=max(1, int(blob.get("policy_version", 1))),
        auto_approve_low=bool(blob.get("auto_approve_low", True)),
        band_low_max=low,
        band_medium_max=medium,
        weights={k: float(weights[k]) for k in WEIGHTS},
        anchors=anchors,
    )


async def resolve_risk_policy(db: AsyncSession) -> RiskPolicy:
    """Effective policy: stored blob over defaults; DB trouble degrades to
    the defaults (policy trouble must never brick scoring or the gate)."""
    try:
        from app.models import PlatformSettings

        row = await db.get(PlatformSettings, 1)
        blob = (getattr(row, "governance", None) or {}) if row else {}
    except Exception:  # noqa: BLE001
        blob = {}
    return _policy_from_blob(blob)


async def update_risk_policy(db: AsyncSession, payload: dict) -> RiskPolicy:
    """Apply a PARTIAL policy update (R3.1): validate hard, merge over the
    stored blob, and bump policy_version exactly once IFF the effective
    policy changed — a no-op PUT must not re-score the world (R2.1).

    `anchors`, when present, REPLACES the override set ({} clears all)."""
    validate_policy_payload(payload)
    from app.models import PlatformSettings

    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(
            id=1, model_allowlist=[], param_bounds={}, rate_limits={}, cost={}
        )
        db.add(row)
    current_blob = dict(getattr(row, "governance", None) or {})
    before = _policy_from_blob(current_blob)

    merged = dict(current_blob)
    for key in ("auto_approve_low", "band_low_max", "band_medium_max", "weights", "anchors"):
        if key in payload and payload[key] is not None:
            merged[key] = payload[key]

    # Cross-field check on the RAW merged values (the blob reader degrades
    # silently by design; the write path must refuse loudly instead).
    low = int(merged.get("band_low_max", DEFAULT_BAND_LOW_MAX))
    medium = int(merged.get("band_medium_max", DEFAULT_BAND_MEDIUM_MAX))
    if not low < medium:
        raise RiskPolicyError("band_low_max must be strictly below band_medium_max")

    after = _policy_from_blob({**merged, "policy_version": before.version})

    def _rounded(weights: dict) -> dict:
        # Proportional identity must survive float paths: 0.3/1.2 and 30/120
        # are the SAME policy (the v3 second computed fill exposed the raw
        # dict comparison's 1e-17 fragility — a no-op PUT must never bump).
        return {k: round(v, 9) for k, v in weights.items()}

    changed = (
        before.auto_approve_low,
        before.band_low_max,
        before.band_medium_max,
        _rounded(before.normalized_weights()),
        before.anchors,
    ) != (
        after.auto_approve_low,
        after.band_low_max,
        after.band_medium_max,
        _rounded(after.normalized_weights()),
        after.anchors,
    )
    merged["policy_version"] = before.version + 1 if changed else before.version
    row.governance = merged
    await db.commit()
    return _policy_from_blob(merged)


def policy_out(policy: RiskPolicy) -> dict:
    """Serialize the effective policy for the admin surface — raw weights as
    stored plus the normalized values actually applied, and the default
    anchor texts so the UI can show placeholders."""
    normalized = policy.normalized_weights()
    return {
        "policy_version": policy.version,
        "auto_approve_low": policy.auto_approve_low,
        "band_low_max": policy.band_low_max,
        "band_medium_max": policy.band_medium_max,
        "weights": policy.weights,
        "weights_effective": {k: round(v, 4) for k, v in normalized.items()},
        "anchors": policy.anchors,
        "default_anchors": DEFAULT_ANCHORS,
        "is_default": policy.version == 1,
    }


def _rubric_system(policy: RiskPolicy) -> str:
    return _RUBRIC_TEMPLATE.format(
        data_sensitivity=policy.anchor("data_sensitivity"),
        user_facing=policy.anchor("user_facing"),
        request_volume=policy.anchor("request_volume"),
    )


def _band(score: int, policy: RiskPolicy | None = None) -> str:
    low = policy.band_low_max if policy else DEFAULT_BAND_LOW_MAX
    medium = policy.band_medium_max if policy else DEFAULT_BAND_MEDIUM_MAX
    if score <= low:
        return "low"
    if score <= medium:
        return "medium"
    return "high"


async def _latest_docs(db: AsyncSession, project_id: uuid.UUID) -> dict[str, str]:
    docs: dict[str, str] = {}
    for doc_type in DOC_ORDER:
        result = await db.execute(
            select(Spec)
            .where(Spec.project_id == project_id, Spec.type == doc_type)
            .order_by(Spec.version.desc())
            .limit(1)
        )
        spec = result.scalar_one_or_none()
        if spec:
            docs[doc_type] = spec.content
    return docs


def content_hash(docs: dict[str, str]) -> str:
    joined = "\n\x00\n".join(f"{k}:{docs.get(k, '')}" for k in DOC_ORDER)
    return hashlib.sha256(joined.encode()).hexdigest()


def _model_capability_factor(models, weights: dict, registry: list[dict]) -> dict:
    ids = {models.chat, models.requirements, models.design, models.tasks}
    tiers = [model["tier"] for model in registry if model["id"] in ids] or [
        "standard"
    ]
    score = max(TIER_CAPABILITY.get(t, 5) for t in tiers)
    strongest = max(tiers, key=lambda t: TIER_CAPABILITY.get(t, 5))
    return {
        "score": score,
        "rationale": f"Strongest resolved model tier is '{strongest}' (computed, not model-assessed).",
        "weight": weights["model_capability"],
    }


def _parse_rubric_json(text: str, weights: dict) -> dict:
    """Strict-ish parse: accept bare JSON or a single fenced block."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fence:
        candidate = fence.group(1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    parsed = json.loads(candidate[start : end + 1])
    out = {}
    for factor in LLM_FACTORS:
        entry = parsed[factor]
        score = int(entry["score"])
        if not 0 <= score <= 10:
            raise ValueError(f"{factor} score {score} out of range")
        out[factor] = {
            "score": score,
            "rationale": str(entry.get("rationale", ""))[:300],
            "weight": weights[factor],
        }
    return out


async def _score_llm_factors(
    docs: dict[str, str],
    model_id: str,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None,
    policy: RiskPolicy,
) -> dict:
    corpus = "\n\n".join(
        f"=== {name}.md ===\n{docs[name][:6000]}" for name in DOC_ORDER if name in docs
    )
    weights = policy.normalized_weights()
    ctx = InvocationCtx(purpose="classification", user_id=user_id, project_id=project_id)
    last_error: Exception | None = None
    for _attempt in range(2):  # one retry on malformed output (R2.4)
        text, _usage, _stop = await converse(
            messages=[{"role": "user", "content": [{"text": corpus}]}],
            system=_rubric_system(policy),
            model_id=model_id,
            max_tokens=600,
            temperature=0.0,
            ctx=ctx,
        )
        try:
            return _parse_rubric_json(text, weights)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning("rubric parse failed (attempt): %s", exc)
    raise ValueError(f"rubric output unparseable after retry: {last_error}")


async def ensure_assessment(
    db: AsyncSession,
    project: Project,
    *,
    user_id: uuid.UUID | None = None,
    wait: bool = True,
) -> AiRiskAssessment | None:
    """Return the assessment for the project's CURRENT content, scoring if new.

    `wait=False` (background triggers) skips out if another scorer holds the
    project lock; `wait=True` (deploy gate) raises ScoringInProgress instead of
    double-scoring.
    """
    docs = await _latest_docs(db, project.id)
    if not docs:
        return None  # nothing to assess; gate treats missing docs as not-deployable anyway
    digest = content_hash(docs)
    # B17: policy is part of the determinism key — same content under the
    # same policy version is scored once; a policy change is a cache miss.
    policy = await resolve_risk_policy(db)

    existing = await db.execute(
        select(AiRiskAssessment).where(
            AiRiskAssessment.project_id == project.id,
            AiRiskAssessment.content_hash == digest,
            AiRiskAssessment.rubric_version == RUBRIC_VERSION,
            AiRiskAssessment.policy_version == policy.version,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None and row.status == "scored":
        return row
    if row is not None and row.status == "error":
        await db.delete(row)  # error rows are retryable — re-score this content
        await db.commit()

    lock = _inflight.setdefault(project.id, asyncio.Lock())
    if lock.locked():
        if wait:
            raise ScoringInProgress(str(project.id))
        return None

    async with lock:
        # Re-check under the lock (another scorer may have just finished)
        recheck = await db.execute(
            select(AiRiskAssessment).where(
                AiRiskAssessment.project_id == project.id,
                AiRiskAssessment.content_hash == digest,
                AiRiskAssessment.rubric_version == RUBRIC_VERSION,
                AiRiskAssessment.policy_version == policy.version,
            )
        )
        row = recheck.scalar_one_or_none()
        if row is not None:
            return row

        weights = policy.normalized_weights()
        template = await db.get(Template, project.template_id) if project.template_id else None
        models = await resolve_models(template)
        registry = await model_registry()
        from app.services.capabilities import capability_reach_score
        from app.services.composition import reach_score

        reach, reach_rationale = reach_score(project.composition)
        cap_reach, cap_rationale = capability_reach_score(
            docs.get("requirements", "")
        )
        factors = {
            "model_capability": _model_capability_factor(
                models, weights, registry
            ),
            "deployment_scope": {
                "score": DEPLOYMENT_SCOPE_SANDBOX,
                "rationale": "Deployments run only in governed, isolated Enclave accounts.",
                "weight": weights["deployment_scope"],
            },
            # Composable agents R4.1: computed from the stored graph — a
            # tool of governance visibility, never model-scored.
            "composition_reach": {
                "score": reach,
                "rationale": reach_rationale,
                "weight": weights["composition_reach"],
            },
            # Agent substance R3.1: declared ladder rungs, computed the same
            # way (rubric v3).
            "capability_reach": {
                "score": cap_reach,
                "rationale": cap_rationale,
                "weight": weights["capability_reach"],
            },
        }
        try:
            factors.update(
                await _score_llm_factors(docs, models.fast, project.id, user_id, policy)
            )
            total = round(
                sum(f["score"] * f["weight"] for f in factors.values()) * 10
            )
            level = _band(total, policy)
            row = AiRiskAssessment(
                project_id=project.id,
                content_hash=digest,
                rubric_version=RUBRIC_VERSION,
                policy_version=policy.version,
                score=total,
                level=level,
                factors=factors,
                status="scored",
                # R1.1: auto-approve is itself policy — off means EVERYTHING queues
                decision=(
                    "auto_approved"
                    if level == "low" and policy.auto_approve_low
                    else "pending"
                ),
                model_id=models.fast,
            )
        except Exception as exc:  # noqa: BLE001 — scoring failure never blocks the trigger op
            logger.exception("risk scoring failed for project %s", project.id)
            row = AiRiskAssessment(
                project_id=project.id,
                content_hash=digest,
                rubric_version=RUBRIC_VERSION,
                policy_version=policy.version,
                factors=factors,
                status="error",
                error=str(exc)[:500],
                model_id=models.fast,
            )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        # S6 workflow: link resubmissions regardless of outcome (an auto-approved
        # revision still belongs to its changes-requested lineage in the timeline);
        # route + notify only when pending (AC-6 ≤5 min by construction).
        from app.services import risk_review

        try:
            await risk_review.link_resubmission(db, row)
            if row.decision == "pending":
                await risk_review.route(db, row)
        except Exception:  # noqa: BLE001 — routing trouble never voids the assessment
            logger.exception("review routing failed for assessment %s", row.id)
        return row


def trigger_assessment(project_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    """Fire-and-forget scoring on save/generation/fork (R2.1). Never raises."""

    async def run() -> None:
        try:
            from app.core.db import SessionLocal

            async with SessionLocal() as db:
                project = await db.get(Project, project_id)
                if project is not None:
                    await ensure_assessment(db, project, user_id=user_id, wait=False)
        except Exception:  # noqa: BLE001
            logger.exception("background risk scoring failed for %s", project_id)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # pragma: no cover — no loop (scripts)
        pass


async def latest_assessment(
    db: AsyncSession, project_id: uuid.UUID
) -> AiRiskAssessment | None:
    result = await db.execute(
        select(AiRiskAssessment)
        .where(AiRiskAssessment.project_id == project_id)
        .order_by(AiRiskAssessment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


class GateBlocked(Exception):
    def __init__(self, code: str, payload: dict):
        self.code = code
        self.payload = payload
        super().__init__(code)


async def deployment_gate(db: AsyncSession, project: Project, user: User) -> AiRiskAssessment:
    """Deploy pre-flight (S4-03 R3): current content must be approved.

    Raises GateBlocked(code=risk_pending|risk_rejected|risk_error) or
    ScoringInProgress; callers map to 403/409.
    """
    assessment = await ensure_assessment(db, project, user_id=user.id, wait=True)
    if assessment is None:
        raise GateBlocked("no_spec", {"detail": "Project has no saved specification to assess"})
    if assessment.status == "error":
        raise GateBlocked(
            "risk_error",
            {"detail": "Automated risk scoring failed; retry or contact an admin",
             "error": assessment.error},
        )
    if assessment.decision in ("auto_approved", "approved"):
        return assessment
    if assessment.decision == "rejected":
        raise GateBlocked(
            "risk_rejected",
            {
                "detail": "Deployment rejected by risk review",
                "score": assessment.score,
                "level": assessment.level,
                "notes": assessment.notes,
            },
        )
    if assessment.decision == "changes_requested":
        raise GateBlocked(
            "risk_changes_requested",
            {
                "detail": "Reviewers requested changes — revise the spec and it will be re-scored",
                "score": assessment.score,
                "level": assessment.level,
                "notes": assessment.notes,
            },
        )
    raise GateBlocked(
        "risk_pending",
        {
            "detail": "Deployment is pending risk review (FSD §4.6.3)",
            "score": assessment.score,
            "level": assessment.level,
        },
    )


async def decide(
    db: AsyncSession,
    assessment: AiRiskAssessment,
    admin: User,
    *,
    approve: bool,
    notes: str | None,
) -> AiRiskAssessment:
    if assessment.decision != "pending":
        raise RiskDecisionError(
            f"Assessment is '{assessment.decision}' — only pending assessments can be decided"
        )
    if not approve and not (notes or "").strip():
        raise RiskDecisionError("Rejection requires notes explaining the decision")
    assessment.decision = "approved" if approve else "rejected"
    assessment.decided_by = admin.id
    assessment.decided_at = datetime.now(UTC)
    assessment.notes = (notes or "").strip() or None
    await db.commit()
    await db.refresh(assessment)
    return assessment


async def queue_counters(db: AsyncSession) -> dict:
    """Queue headline counts in ONE grouped pass (S13-02: was three sequential
    COUNT round-trips on the admin queue's hot path)."""
    since = datetime.now(UTC) - timedelta(days=30)
    rows = (
        await db.execute(
            select(
                func.count().filter(AiRiskAssessment.decision == "pending"),
                func.count().filter(
                    AiRiskAssessment.decision == "approved",
                    AiRiskAssessment.decided_at >= since,
                ),
                func.count().filter(
                    AiRiskAssessment.decision == "rejected",
                    AiRiskAssessment.decided_at >= since,
                ),
            ).select_from(AiRiskAssessment)
        )
    ).one()
    return {"pending": rows[0], "approved_30d": rows[1], "rejected_30d": rows[2]}
