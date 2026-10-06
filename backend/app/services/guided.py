"""Guided mode state machine (FSD §4.1.2, guided-mode spec).

The server owns guided_state; the client renders it and posts answers. Steps:
use_case → context → behavior → clarify → generate → done. The clarification
round uses the fast model at temperature 0 with strict JSON, capped at 3
rounds / 15 questions; unanswered gaps become explicit assumptions. The final
brief seeds the EXISTING generation pipeline — no parallel path.
"""

import copy
import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatSession, Template, User
from app.services.bedrock import InvocationCtx, converse
from app.services.guardrails import resolve_models

logger = logging.getLogger("marshal.guided")

STEPS = ("use_case", "context", "behavior", "clarify", "generate", "done")
FORM_STEPS = ("use_case", "context", "behavior")
MAX_ROUNDS = 3
MAX_QUESTIONS = 15

CATEGORIES = (
    "chatbot", "document_processing", "data_analysis",
    "workflow_automation", "content_generation", "other",
)

CLARIFY_SYSTEM = """You review answers a business user gave about an AI application they want built.
Identify the most important information gaps for writing a specification. Ask 2-5 targeted follow-up questions — plain language, one topic each, no jargon.
If the answers are already sufficient to draft a reasonable specification, proceed instead of asking.

Respond with ONLY a JSON object, exactly one of:
{"questions": ["...", "..."]}
{"proceed": true}"""


class GuidedStateError(Exception):
    """Invalid step/answer submission — surfaces as 422."""


class UseCaseAnswers(BaseModel):
    problem: str = Field(min_length=20, max_length=4000)
    category: str
    category_other: str | None = Field(default=None, max_length=200)

    def check(self) -> None:
        if self.category not in CATEGORIES:
            raise GuidedStateError(f"Unknown category '{self.category}'")
        if self.category == "other" and not (self.category_other or "").strip():
            raise GuidedStateError("Describe your category when choosing 'other'")


class ContextAnswers(BaseModel):
    audience: str = Field(pattern="^(customers|internal|both)$")
    data_sources: list[str] = Field(min_length=1)
    sensitive_data: str = Field(pattern="^(pii|financial|health|none|unsure)$")

    def check(self) -> None:
        allowed = {"documents", "apis", "databases", "user_input", "files"}
        unknown = set(self.data_sources) - allowed
        if unknown:
            raise GuidedStateError(f"Unknown data sources: {', '.join(sorted(unknown))}")


class BehaviorAnswers(BaseModel):
    key_actions: str = Field(min_length=10, max_length=4000)
    constraints: str | None = Field(default=None, max_length=2000)

    def check(self) -> None:
        return None


STEP_SCHEMAS = {"use_case": UseCaseAnswers, "context": ContextAnswers, "behavior": BehaviorAnswers}


def initial_guided_state() -> dict:
    return {
        "step": "use_case",
        "answers": {},
        "clarification": {"rounds": 0, "questions_total": 0, "items": []},
        "assumptions": [],
        "outcome": None,  # completed | proceeded_with_assumptions
    }


def _require_guided(session: ChatSession) -> dict:
    if session.mode != "guided" or session.guided_state is None:
        raise GuidedStateError("Session is not in guided mode")
    # Deep copy: in-place mutation of the loaded JSONB would make the ORM's
    # old/new equality check pass and silently skip the UPDATE on commit.
    return copy.deepcopy(session.guided_state)


def advance_step(session: ChatSession, step: str, answers: dict) -> dict:
    """Validate + merge a form step; back-navigation = resubmitting an earlier step."""
    state = _require_guided(session)
    if step not in FORM_STEPS:
        raise GuidedStateError(f"'{step}' is not a form step")
    current = state["step"]
    if current not in FORM_STEPS or (
        FORM_STEPS.index(step) > FORM_STEPS.index(current) if current in FORM_STEPS else True
    ):
        # Allowed: resubmit current or any EARLIER step (lossless back-nav, R1.4)
        if current in ("clarify", "generate", "done"):
            raise GuidedStateError("Form steps are frozen once clarification starts")
        if current in FORM_STEPS and FORM_STEPS.index(step) > FORM_STEPS.index(current):
            raise GuidedStateError(f"Cannot skip ahead to '{step}' from '{current}'")
    schema = STEP_SCHEMAS[step]
    try:
        parsed = schema(**answers)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise GuidedStateError(
            f"{step}.{'.'.join(str(x) for x in first['loc'])}: {first['msg']}"
        ) from exc
    parsed.check()
    state["answers"][step] = parsed.model_dump()
    # Advance only when submitting the frontier step
    if step == state["step"]:
        next_index = FORM_STEPS.index(step) + 1
        state["step"] = FORM_STEPS[next_index] if next_index < len(FORM_STEPS) else "clarify"
    return state


def _parse_clarify_json(text: str) -> dict:
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fence:
        candidate = fence.group(1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in clarify response")
    parsed = json.loads(candidate[start : end + 1])
    if parsed.get("proceed") is True:
        return {"proceed": True}
    questions = parsed.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("clarify response missing questions")
    return {"questions": [str(q)[:400] for q in questions[:5]]}


async def run_clarify_round(db: AsyncSession, session: ChatSession, user: User) -> dict:
    """Ask the next round of questions, or proceed (R2.2). Never strands the wizard."""
    state = _require_guided(session)
    if state["step"] != "clarify":
        raise GuidedStateError("Session is not at the clarification step")
    clar = state["clarification"]
    if clar["rounds"] >= MAX_ROUNDS or clar["questions_total"] >= MAX_QUESTIONS:
        return await freeze_assumptions(db, session, reason="caps_reached")

    template = await db.get(Template, session.template_id) if session.template_id else None
    models = await resolve_models(template)
    transcript = json.dumps(
        {"answers": state["answers"],
         "already_asked": [i["question"] for i in clar["items"]],
         "previous_answers": [
             {"q": i["question"], "a": i["answer"] or "(skipped)"} for i in clar["items"]
         ]},
        indent=1,
    )
    try:
        text, _usage, _stop = await converse(
            messages=[{"role": "user", "content": [{"text": transcript}]}],
            system=CLARIFY_SYSTEM,
            model_id=models.fast,
            max_tokens=400,
            temperature=0.0,
            ctx=InvocationCtx(
                purpose="classification", user_id=user.id, session_id=session.id
            ),
        )
        parsed = _parse_clarify_json(text)
    except Exception:  # noqa: BLE001 — one retry, then proceed with assumptions (R2.4)
        logger.warning("clarify round failed; retrying once", exc_info=True)
        try:
            text, _usage, _stop = await converse(
                messages=[{"role": "user", "content": [{"text": transcript}]}],
                system=CLARIFY_SYSTEM,
                model_id=models.fast,
                max_tokens=400,
                temperature=0.0,
                ctx=InvocationCtx(
                    purpose="classification", user_id=user.id, session_id=session.id
                ),
            )
            parsed = _parse_clarify_json(text)
        except Exception:  # noqa: BLE001
            logger.exception("clarify retry failed — proceeding with assumptions")
            return await freeze_assumptions(db, session, reason="clarify_failed")

    if parsed.get("proceed"):
        return await freeze_assumptions(db, session, reason="model_satisfied")

    room = MAX_QUESTIONS - clar["questions_total"]
    questions = parsed["questions"][:room]
    round_number = clar["rounds"] + 1
    for question in questions:
        clar["items"].append(
            {"id": f"q{clar['questions_total'] + 1}", "round": round_number,
             "question": question, "answer": None, "skipped": False}
        )
        clar["questions_total"] += 1
    clar["rounds"] = round_number
    state["clarification"] = clar
    session.guided_state = state
    await db.commit()
    await db.refresh(session)
    return state


def record_clarify_answers(session: ChatSession, answers: list[dict]) -> dict:
    """Store answers/skips for the current round's questions."""
    state = _require_guided(session)
    items = {i["id"]: i for i in state["clarification"]["items"]}
    for entry in answers:
        item = items.get(entry.get("question_id"))
        if item is None:
            raise GuidedStateError(f"Unknown question id '{entry.get('question_id')}'")
        if entry.get("skip"):
            item["skipped"] = True
            item["answer"] = None
        else:
            answer = str(entry.get("answer") or "").strip()
            if not answer:
                raise GuidedStateError(f"Provide an answer or skip for '{item['id']}'")
            item["answer"] = answer[:2000]
            item["skipped"] = False
    return state


async def freeze_assumptions(
    db: AsyncSession, session: ChatSession, reason: str
) -> dict:
    """Unanswered gaps → explicit assumptions; step → generate (R2.2/R2.3)."""
    state = _require_guided(session)
    assumptions = list(state.get("assumptions") or [])
    for item in state["clarification"]["items"]:
        if item["answer"] is None:
            assumptions.append(
                f"No answer provided for: \"{item['question']}\" — the specification "
                "will use a sensible default."
            )
    answers = state["answers"]
    if answers.get("context", {}).get("sensitive_data") == "unsure":
        assumptions.append(
            "User was unsure about sensitive data — assumed personal data MAY be present; "
            "the design must include PII-safe handling."
        )
    state["assumptions"] = assumptions
    state["outcome"] = "completed" if reason == "model_satisfied" else "proceeded_with_assumptions"
    state["step"] = "generate"
    session.guided_state = state
    await db.commit()
    await db.refresh(session)
    return state


def build_brief(state: dict) -> str:
    """Deterministic markdown brief — the user message that seeds generation (R3.1)."""
    answers = state["answers"]
    use_case, context, behavior = (
        answers.get("use_case", {}), answers.get("context", {}), answers.get("behavior", {}),
    )
    category = use_case.get("category", "other")
    if category == "other":
        category = f"other — {use_case.get('category_other', '')}"
    lines = [
        "# Application Brief (guided intake)",
        "",
        "## Problem",
        use_case.get("problem", ""),
        "",
        f"## Category\n{category}",
        "",
        f"## Audience\n{ {'customers': 'External customers', 'internal': 'Internal staff', 'both': 'Both external customers and internal staff'}.get(context.get('audience'), context.get('audience', '')) }",
        "",
        "## Data & Sensitivity",
        f"Data sources: {', '.join(context.get('data_sources', []))}",
        f"Sensitive data: {context.get('sensitive_data', 'unknown')}",
        "",
        "## Key Actions",
        behavior.get("key_actions", ""),
    ]
    if behavior.get("constraints"):
        lines += ["", "## Constraints", behavior["constraints"]]
    answered = [
        i for i in state["clarification"]["items"] if i["answer"]
    ]
    if answered:
        lines += ["", "## Clarifications"]
        lines += [f"- Q: {i['question']}\n  A: {i['answer']}" for i in answered]
    if state.get("assumptions"):
        lines += ["", "## Stated Assumptions"]
        lines += [f"- {a}" for a in state["assumptions"]]
    return "\n".join(lines)


async def prepare_generation(db: AsyncSession, session: ChatSession, user: User) -> str:
    """Persist the brief as the session's user message so the EXISTING
    generate-spec flow (which reads chat history) has its transcript."""
    from app.services import chat as chat_service

    state = _require_guided(session)
    if state["step"] != "generate":
        raise GuidedStateError("Wizard has not reached the generation step")
    brief = build_brief(state)
    history = await chat_service.get_messages(session.id)
    if not any(m["content"] == brief for m in history):
        await chat_service.put_message(session.id, "user", brief)
    return brief


async def switch_to_freeform(db: AsyncSession, session: ChatSession, user: User) -> ChatSession:
    """Power/admin escape hatch (R1.2): summary lands in chat, state freezes."""
    from app.services import chat as chat_service

    if user.role != "admin" and user.persona != "power":
        raise GuidedStateError("Switching to freeform chat is a Power User capability")
    state = _require_guided(session)
    summary = build_brief(state) if state["answers"] else "(guided wizard exited before any answers)"
    await chat_service.put_message(
        session.id, "assistant",
        "Switched to freeform chat. Here is everything collected in the guided wizard so far:\n\n"
        + summary,
    )
    session.mode = "freeform"
    await db.commit()
    await db.refresh(session)
    return session


def mark_done(session: ChatSession) -> dict:
    state = _require_guided(session)
    state["step"] = "done"
    return state
