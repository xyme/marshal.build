"""Full spec-set generation jobs (S2-01): requirements → design → tasks.

Runs as an in-process background task (same operational pattern as the
deployment orchestrator): SpecGeneration row is the durable state, an in-memory
event bus feeds SSE subscribers, and a startup rehydrator fails orphaned jobs.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import ChatSession, Project, Spec, SpecGeneration, Template, User
from app.services import chat as chat_service
from app.services import spec_validation as sv
from app.services.bedrock import InvocationCtx, converse
from app.services.deployment import DeploymentEventBus
from app.services.guardrails import ResolvedModels, resolve_models, template_prompt_block

logger = logging.getLogger("marshal.specgen")

generation_bus = DeploymentEventBus()  # same pub/sub semantics; keyed by generation id
_tasks: dict[str, asyncio.Task] = {}

# Full-set budget (spec R2.3). 300 → 420 (13.5AB): a single 180 s Bedrock
# stall on a DOCUMENT call must be survivable for a large three-document set
# (live arithmetic 30 Sep 2026: 180 + 53 + 59 + 45 = 337 s). Healthy runs
# finish in 131–146 s (30-day p50–max); this is a safety net, not a target.
GENERATION_TIMEOUT_S = 420
# The project-name call is cosmetic: 30 tokens from the fast model. It must
# never be able to spend the generation budget — live it stalled 183 s and
# starved tasks.md (13.5AB). Past this cap the deterministic fallback wins.
TITLE_TIMEOUT_S = 12
TITLE_MAX_WORDS = 6
NAME_SYSTEM = (
    "Given this conversation about an app idea, reply with ONLY a short project name "
    "(2-5 words, no quotes, no punctuation). Never explain, ask, or refuse — if the "
    "conversation is unclear, invent a plausible short name anyway."
)


def _now() -> datetime:
    return datetime.now(UTC)


class GenerationInProgress(Exception):
    pass


# ------------------------------------------------------------------ helpers


async def _next_version(db: AsyncSession, project_id: uuid.UUID, doc_type: str) -> int:
    current = await db.scalar(
        select(func.max(Spec.version)).where(
            Spec.project_id == project_id, Spec.type == doc_type
        )
    )
    return (current or 0) + 1


async def latest_spec(db: AsyncSession, project_id: uuid.UUID, doc_type: str) -> Spec | None:
    result = await db.execute(
        select(Spec)
        .where(Spec.project_id == project_id, Spec.type == doc_type)
        .order_by(Spec.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def valid_project_name(suggestion: str) -> str | None:
    """Accept the model's suggestion only if it is shaped like a NAME.

    Live (13.5AB) the fast model answered the naming prompt conversationally
    ("I don't see any attached specs in your message. However, …") and the
    first 80 chars became the project name. A name is one line, ≤6 words,
    no sentence punctuation.
    """
    text = " ".join((suggestion or "").strip().strip("\"'`").split())
    if not text or len(text) > 80:
        return None
    if "\n" in suggestion.strip() or any(ch in text for ch in ".?!:;"):
        return None
    if len(text.split()) > TITLE_MAX_WORDS:
        return None
    return text


def fallback_project_name(session: ChatSession, transcript: str) -> str:
    """Deterministic name when the model can't be asked or answered badly:
    the session title if the user set one, else the first words of the
    opening user message, else a constant."""
    title = (session.title or "").strip()
    if title and title.lower() not in ("new chat", "new session", "untitled", "untitled project"):
        return title[:80]
    first_user = ""
    for chunk in transcript.split("\n\n"):
        if chunk.startswith("[user]: "):
            first_user = chunk[len("[user]: "):]
            break
    words = [w.strip("\"'`.,;:!?()[]{}") for w in first_user.split()]
    words = [w for w in words if w][:5]
    if words:
        return " ".join(words)[:80]
    return "Untitled project"


async def _ensure_project(
    db: AsyncSession, user: User, session: ChatSession, transcript: str, models: ResolvedModels
) -> Project:
    if session.project_id:
        project = await db.get(Project, session.project_id)
        if project:
            return project
    name = fallback_project_name(session, transcript)
    try:
        suggestion, _, _ = await asyncio.wait_for(
            converse(
                messages=[{"role": "user", "content": [{"text": transcript[-6000:]}]}],
                system=NAME_SYSTEM,
                model_id=models.fast,
                max_tokens=30,
                ctx=InvocationCtx(
                    purpose="title", user_id=user.id, session_id=session.id
                ),
            ),
            timeout=TITLE_TIMEOUT_S,
        )
        validated = valid_project_name(suggestion)
        if validated:
            name = validated
        else:
            logger.warning(
                "Project name suggestion rejected (not name-shaped); using fallback %r", name
            )
    except TimeoutError:
        logger.warning(
            "Project name suggestion exceeded %ss; using fallback %r", TITLE_TIMEOUT_S, name
        )
    except Exception:  # noqa: BLE001 — best-effort naming
        logger.warning("Project name suggestion failed; using fallback %r", name)
    project = Project(
        user_id=user.id, name=name, status="draft", template_id=session.template_id
    )
    db.add(project)
    await db.flush()
    if session.template_id:
        template = await db.get(Template, session.template_id)
        if template:
            template.usage_count = (template.usage_count or 0) + 1
    session.project_id = project.id
    await db.commit()
    await db.refresh(project)
    return project


def _doc_user_prompt(doc_type: str, transcript: str, context_docs: dict[str, str]) -> str:
    if doc_type == "requirements":
        return (
            "Here is the full conversation about the application to specify:\n\n"
            f"{transcript}\n\nProduce the requirements document now."
        )
    if doc_type == "design":
        return (
            "Finalized requirements document:\n\n"
            f"{context_docs['requirements']}\n\n"
            "Conversation context (for nuance):\n\n"
            f"{transcript[-20000:]}\n\nProduce the design document now."
        )
    return (
        "Finalized requirements document:\n\n"
        f"{context_docs['requirements']}\n\n"
        "Finalized design document:\n\n"
        f"{context_docs['design']}\n\nProduce the implementation plan now."
    )


async def _generate_doc(
    doc_type: str,
    transcript: str,
    context_docs: dict[str, str],
    models: ResolvedModels,
    template: Template | None,
    ctx: InvocationCtx | None = None,
    substrate_block: str = "",
    connectors_block: str = "",
) -> str:
    model_id = getattr(models, doc_type)
    # Substrate rides the SYSTEM prompt (brownfield-substrate R3): the
    # transcript seam would miss the tasks doc entirely and get sliced by the
    # design doc's 20k transcript tail. This one concat reaches all three doc
    # types and both job shapes (all-docs + per-doc regenerate + guided).
    system = (
        sv.system_prompt_for(doc_type)
        + template_prompt_block(template)
        + substrate_block
        + connectors_block
    )
    content, _usage, stop_reason = await converse(
        messages=[
            {"role": "user", "content": [{"text": _doc_user_prompt(doc_type, transcript, context_docs)}]}
        ],
        system=system,
        model_id=model_id,
        max_tokens=models.max_tokens_generation,
        ctx=ctx,
    )
    if stop_reason == "max_tokens":
        # Truncated output can never validate — surface the real cause (template
        # max_tokens too small for a full document) instead of a validation error.
        raise ValueError(
            f"{doc_type} generation hit the max_tokens cap ({models.max_tokens}) before "
            "completing. Raise the template's model.max_tokens (4096 recommended)."
        )
    content = sv.strip_fences(content)
    if not sv.is_valid(doc_type, content):
        logger.warning("%s failed validation; repair pass", doc_type)
        content, _, repair_stop = await converse(
            messages=[{"role": "user", "content": [{"text": content or "(empty)"}]}],
            system=sv.REPAIR_SYSTEM.format(doc_type=doc_type),
            model_id=model_id,
            max_tokens=models.max_tokens_generation,
            ctx=ctx,
        )
        if repair_stop == "max_tokens":
            raise ValueError(
                f"{doc_type} repair pass also hit the max_tokens cap ({models.max_tokens}). "
                "Raise the template's model.max_tokens."
            )
        content = sv.strip_fences(content)
    problems = sv.validate_doc(doc_type, content)
    if problems:
        raise ValueError(f"{doc_type} failed validation after repair: {'; '.join(problems)}")
    return content


# ------------------------------------------------------------------ job API


async def start_generation(
    db: AsyncSession, user: User, session: ChatSession, only_type: str | None = None
) -> SpecGeneration:
    if only_type and only_type not in sv.DOC_TYPES:
        raise ValueError(f"Unknown document type: {only_type}")
    history = await chat_service.get_messages(session.id)
    if not history:
        raise ValueError("Session has no messages to generate from")

    running = await db.scalar(
        select(func.count())
        .select_from(SpecGeneration)
        .where(SpecGeneration.session_id == session.id, SpecGeneration.status == "running")
    )
    if running:
        raise GenerationInProgress("A generation is already running for this session")

    doc_types = [only_type] if only_type else list(sv.DOC_TYPES)
    generation = SpecGeneration(
        session_id=session.id,
        project_id=session.project_id,
        status="running",
        docs=[{"type": t, "status": "pending"} for t in doc_types],
    )
    db.add(generation)
    session.status = "generating"
    await db.commit()
    await db.refresh(generation)

    task = asyncio.create_task(_run_generation(generation.id, user.id, only_type))
    _tasks[str(generation.id)] = task
    task.add_done_callback(lambda t: _tasks.pop(str(generation.id), None))
    return generation


async def _set_doc(db: AsyncSession, generation: SpecGeneration, doc_type: str, **fields) -> None:
    generation.docs = [
        {**d, **fields} if d["type"] == doc_type else d for d in generation.docs
    ]
    await db.commit()
    await generation_bus.publish(
        str(generation.id),
        {"type": f"doc_{fields.get('status', 'update')}", "doc": doc_type, **fields},
    )


async def _run_generation(
    generation_id: uuid.UUID, user_id: uuid.UUID, only_type: str | None
) -> None:
    async with SessionLocal() as db:
        generation = await db.get(SpecGeneration, generation_id)
        if generation is None:
            return
        session = (
            await db.get(ChatSession, generation.session_id)
            if generation.session_id is not None
            else None
        )
        if session is None:
            # 13.5AB: the session was deleted between dispatch and start
            # (session_id detached via SET NULL). Nothing to generate against.
            generation.status = "failed"
            generation.error = "Session was deleted before generation started"
            generation.finished_at = _now()
            await db.commit()
            return
        user = await db.get(User, user_id)
        try:
            async with asyncio.timeout(GENERATION_TIMEOUT_S):
                history = await chat_service.get_messages(session.id)
                transcript = "\n\n".join(
                    f"[{m['role']}]: {m['content']}" for m in history
                )
                template = (
                    await db.get(Template, session.template_id)
                    if session.template_id
                    else None
                )
                # Brownfield grounding (spec R3): computed once, injected into
                # every document's system prompt below.
                substrate_block = chat_service.substrate_prompt_block(session)
                # C2 connector grounding (flag-gated, metadata-only) rides the
                # same seam so generated requirements can declare real slugs.
                from app.services import connectors as connectors_svc

                connectors_block = await connectors_svc.connector_grounding_block(db)
                models = await resolve_models(template)
                project = await _ensure_project(db, user, session, transcript, models)
                if generation.project_id is None:
                    generation.project_id = project.id
                    await db.commit()

                doc_types = [only_type] if only_type else list(sv.DOC_TYPES)
                context_docs: dict[str, str] = {}
                # Single-doc regenerate uses latest stored versions of the others (R3.2)
                if only_type:
                    for other in sv.DOC_TYPES:
                        if other != only_type:
                            existing = await latest_spec(db, project.id, other)
                            if existing:
                                context_docs[other] = existing.content

                DOC_DEPENDENCIES: dict[str, list[str]] = {
                    "requirements": [],
                    "design": ["requirements"],
                    "tasks": ["requirements", "design"],
                }
                failures: list[str] = []
                for doc_type in doc_types:
                    # Skip (don't crash) documents whose upstream context failed (R1.5)
                    missing = [
                        dep for dep in DOC_DEPENDENCIES[doc_type] if dep not in context_docs
                    ]
                    if missing:
                        failures.append(doc_type)
                        await _set_doc(
                            db,
                            generation,
                            doc_type,
                            status="failed",
                            error=f"Skipped: depends on {', '.join(missing)} which did not generate",
                        )
                        continue
                    await _set_doc(db, generation, doc_type, status="generating")
                    try:
                        content = await _generate_doc(
                            doc_type,
                            transcript,
                            context_docs,
                            models,
                            template,
                            substrate_block=substrate_block,
                            connectors_block=connectors_block,
                            ctx=InvocationCtx(
                                purpose=doc_type,
                                user_id=user.id,
                                project_id=project.id,
                                session_id=session.id,
                                generation_id=generation.id,
                            ),
                        )
                        if doc_type == "requirements":
                            # Composable agents R1.2: generated requirements
                            # sync the graph; a CompositionError fails THIS
                            # doc with its named detail (caught below) — a
                            # cycle must not land silently via generation.
                            from app.services.composition import (
                                sync_project_composition,
                            )

                            await sync_project_composition(db, project, content)
                            # Agent substance R3.2: a generated rung the
                            # template forbids fails THIS doc by name too.
                            from app.services.capabilities import (
                                enforce_template_rail,
                            )

                            await enforce_template_rail(db, project, content)
                        spec = Spec(
                            project_id=project.id,
                            session_id=session.id,
                            version=await _next_version(db, project.id, doc_type),
                            type=doc_type,
                            content=content,
                            model_id=getattr(models, doc_type),
                            origin="generated",
                        )
                        db.add(spec)
                        await db.flush()
                        context_docs[doc_type] = content
                        await _set_doc(
                            db,
                            generation,
                            doc_type,
                            status="done",
                            spec_id=str(spec.id),
                            version=spec.version,
                        )
                    except Exception as exc:  # noqa: BLE001 — continue other docs (R1.5)
                        logger.exception("Generation of %s failed", doc_type)
                        failures.append(doc_type)
                        await _set_doc(
                            db, generation, doc_type, status="failed", error=str(exc)[:500]
                        )

                # Project status: spec_complete only when all three types exist (R1.6)
                have_all = True
                for doc_type in sv.DOC_TYPES:
                    if await latest_spec(db, project.id, doc_type) is None:
                        have_all = False
                        break
                if have_all and project.status == "draft":
                    project.status = "spec_complete"
                generation.status = "failed" if failures else "done"
                generation.error = (
                    f"Failed documents: {', '.join(failures)}" if failures else None
                )
                generation.finished_at = _now()
                session.status = "preview" if not failures else "chatting"
                await db.commit()
                if not failures:
                    # Risk scoring on generation completion (S4-02 R2.1)
                    from app.services.risk import trigger_assessment

                    trigger_assessment(project.id, user_id)
                    # Notify owner (notifications spec, FSD §4.4.6)
                    from app.services import notifications as notif

                    notif.emit(
                        notif.emit_for_user(
                            user_id,
                            type="generation_complete",
                            title="Your specification is ready",
                            body=f'All documents generated for "{project.name}".',
                            link=f"/projects/{project.id}?tab=spec",
                        )
                    )
        except TimeoutError:
            generation.status = "failed"
            generation.error = f"Generation exceeded {GENERATION_TIMEOUT_S}s budget"
            generation.finished_at = _now()
            session.status = "chatting"
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Generation job %s crashed", generation_id)
            generation.status = "failed"
            generation.error = str(exc)[:500]
            generation.finished_at = _now()
            session.status = "chatting"
            await db.commit()
        finally:
            await generation_bus.publish(
                str(generation_id), {"type": "done", "status": generation.status}
            )


async def get_latest_generation(
    db: AsyncSession, session_id: uuid.UUID
) -> SpecGeneration | None:
    result = await db.execute(
        select(SpecGeneration)
        .where(SpecGeneration.session_id == session_id)
        .order_by(SpecGeneration.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def rehydrate_inflight_generations() -> None:
    """Backend restarts kill in-process jobs — mark them failed-retriable (R2.4)."""
    try:
        async with SessionLocal() as db:
            result = await db.execute(
                select(SpecGeneration).where(SpecGeneration.status == "running")
            )
            for generation in result.scalars():
                generation.status = "failed"
                generation.error = "Interrupted by a platform restart — please retry"
                generation.finished_at = _now()
                generation.docs = [
                    {**d, "status": "failed"} if d["status"] in ("pending", "generating") else d
                    for d in generation.docs
                ]
                session = (
                    await db.get(ChatSession, generation.session_id)
                    if generation.session_id is not None
                    else None
                )
                if session and session.status == "generating":
                    session.status = "chatting"
            await db.commit()
    except Exception as exc:  # pragma: no cover — first boot, no tables yet
        logger.warning("Generation rehydration skipped: %s", exc)
