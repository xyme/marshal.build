"""Chat + full spec-set generation endpoints (S1-03/04, S2-01, S2-06, S2-07)."""

import asyncio
import base64
import binascii
import json
import logging
import uuid

from botocore.exceptions import ReadTimeoutError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.auth import get_current_user, require_admin_security_if_admin
from app.core.db import get_db
from app.models import ChatSession, Project, Spec, Template, User
from app.schemas.chat import (
    DocSpecState,
    EffectiveParamsOut,
    FullSpecResponse,
    GenerationOut,
    GuidedAnswerIn,
    GuidedClarifyIn,
    MessageIn,
    MessageOut,
    PromptContextOut,
    PromptSegment,
    SessionCreate,
    SessionOut,
    SessionUpdate,
    SpecOut,
    SpecVersionInfo,
    SubstrateIn,
)
from app.services import analytics, audit, specgen
from app.services import chat as chat_service
from app.services import guided as guided_service
from app.services.bedrock import (
    InvocationCtx,
    StreamResult,
    preflight_model_call,
    stream_converse,
)
from app.services.guardrails import (
    GuardrailViolation,
    resolve_models,
    resolve_session_call,
    template_prompt_block,
)
from app.services.spec_validation import DOC_TYPES

logger = logging.getLogger("marshal.chat")
router = APIRouter(prefix="/chat", tags=["chat"])

# Shown when the Bedrock stream stops mid-reply (read timeout AFTER the first
# token); the partial text is persisted as a truncated assistant message.
STREAM_STALL_MESSAGE = (
    "The model stopped responding mid-reply; the partial answer was kept — "
    "send again to continue."
)


async def _owned_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Creator or project editor+ (collaboration spec R3.2)."""
    session = await chat_service.get_accessible_session(db, user, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    request: Request,
    payload: SessionCreate | None = None,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    try:
        session = await chat_service.create_session(
            db,
            user,
            template_id=payload.template_id if payload else None,
            mode=payload.mode if payload else None,
            project_id=payload.project_id if payload else None,
        )
    except chat_service.PersonaModeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.set_audit_detail(request, resource_id=str(session.id), mode=session.mode)
    return session


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SessionOut]:
    sessions = await chat_service.list_sessions(db, user)
    # Creator + project labels for shared-project sessions (R3.2)
    creator_ids = {s.user_id for s in sessions if s.user_id != user.id}
    names: dict[uuid.UUID, str] = {}
    if creator_ids:
        rows = await db.execute(select(User).where(User.id.in_(creator_ids)))
        names = {u.id: (u.name or u.email.split("@")[0]) for u in rows.scalars()}
    project_ids = {s.project_id for s in sessions if s.project_id}
    project_names: dict[uuid.UUID, str] = {}
    if project_ids:
        rows = await db.execute(select(Project).where(Project.id.in_(project_ids)))
        project_names = {p.id: p.name for p in rows.scalars()}
    out = []
    for s in sessions:
        item = SessionOut.model_validate(s)
        item.is_mine = s.user_id == user.id
        item.creator_name = None if item.is_mine else names.get(s.user_id)
        item.project_name = project_names.get(s.project_id) if s.project_id else None
        out.append(item)
    return out


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session: ChatSession = Depends(_owned_session)) -> ChatSession:
    return session


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(session: ChatSession = Depends(_owned_session)) -> list[MessageOut]:
    items = await chat_service.get_messages(session.id)
    return [
        MessageOut(role=i["role"], content=i["content"], model_id=i.get("model_id"), sk=i["sk"])
        for i in items
    ]


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def update_session(
    payload: SessionUpdate,
    request: Request,
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Rename and/or set requested rail overrides (S18 R3.3, existing audit row)."""
    if payload.title is not None:
        session.title = payload.title
    rail_fields = {"model_id": payload.model_id, "temperature": payload.temperature,
                   "max_tokens": payload.max_tokens}
    changed = {k: v for k, v in rail_fields.items() if v is not None}
    if changed:
        if payload.model_id is not None:
            template = await db.get(Template, session.template_id) if session.template_id else None
            models = await resolve_models(template)
            if payload.model_id not in models.allowed:
                raise HTTPException(
                    status_code=422,
                    detail=f"Model '{payload.model_id}' is not allowed for this session "
                    "(platform allowlist ∩ template rails).",
                )
        session.params = {**(session.params or {}), **changed}
        audit.set_audit_detail(request, params=changed)
    await db.commit()
    await db.refresh(session)
    return session


def _substrate_text(document_b64: str) -> str:
    """Decode + extract a single substrate document (pdf-docx-ingestion R3.3):
    PDF/DOCX extracted server-side, UTF-8 text accepted as-is, archives
    refused by name (spec sets are projects, not substrate). Raises
    HTTPException(422) directly — the endpoint's contract."""
    import io
    import zipfile

    from app.services import doc_extract

    def _err(message: str) -> HTTPException:
        return HTTPException(status_code=422, detail=message)

    try:
        raw = base64.b64decode(document_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _err("document_b64 is not valid base64") from exc
    try:
        if raw[:5] == doc_extract.PDF_MAGIC:
            return doc_extract.extract_pdf_text(raw, max_chars=200_000)
        if raw[:4] == doc_extract.ZIP_MAGIC:
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile as exc:
                raise doc_extract.DocExtractionError(doc_extract.ERR_CORRUPT) from exc
            if doc_extract.is_docx(zf):
                return doc_extract.extract_docx_text(raw, max_chars=200_000)
            raise _err("Substrate accepts a single document, not an archive")
    except doc_extract.DocExtractionError as exc:
        raise _err(str(exc)) from exc
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise _err(
            "Source is not a supported format — expected a PDF, a Word .docx "
            "document, or UTF-8 text"
        ) from exc


@router.put("/sessions/{session_id}/substrate", response_model=SessionOut)
async def attach_substrate(
    payload: SubstrateIn,
    request: Request,
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Attach or replace the brownfield substrate (brownfield-substrate R1).

    Audit carries size + source, NEVER content — model-call audit already
    captures prompt text where that is policy. pdf-docx-ingestion R1.3:
    document_b64 carries a single PDF/DOCX (or UTF-8 text) the server
    extracts; the stored substrate stays TEXT under the same 200KB cap.
    """
    if bool(payload.content) == bool(payload.document_b64):
        raise HTTPException(
            status_code=422, detail="Provide exactly one of content or document_b64"
        )
    if payload.document_b64:
        content = await asyncio.to_thread(_substrate_text, payload.document_b64)
    else:
        content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="Substrate content is empty")
    if len(content) > 200_000:
        raise HTTPException(
            status_code=422, detail="Extracted text exceeds 200000 characters"
        )
    session.substrate = content
    session.substrate_source = (payload.source or "").strip() or None
    await db.commit()
    await db.refresh(session)
    audit.set_audit_detail(request, size=len(content), source=session.substrate_source)
    return session


@router.delete("/sessions/{session_id}/substrate", response_model=SessionOut)
async def remove_substrate(
    request: Request,
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Remove the substrate; brownfield framing is gone from the next message."""
    audit.set_audit_detail(
        request, size=session.substrate_size, source=session.substrate_source
    )
    session.substrate = None
    session.substrate_source = None
    await db.commit()
    await db.refresh(session)
    return session


def _studio_readable(user: User) -> None:
    """Server-side studio gating (D17 + dark launch): business personas never;
    non-admins only when the flag is on. Defense in depth behind the route.
    Admins bypass the persona block — dark-launch preview must always work
    (live S17 drill finding: the demo admin carries persona=business)."""
    if user.persona == "business" and user.role != "admin":
        raise HTTPException(status_code=403, detail="The studio is a power-user surface")


async def _studio_flag_gate(user: User) -> None:
    from app.services.platform_settings import get_controls

    _studio_readable(user)
    controls = await get_controls()
    if not controls.feature_flags.get("studio_enabled", False) and user.role != "admin":
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/sessions/{session_id}/prompt-context", response_model=PromptContextOut)
async def prompt_context(
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> PromptContextOut:
    """The system-prompt blocks the NEXT message would carry (studio spec R1.1).

    Pure read: no model call, nothing persisted. Demystifies generation by
    showing persona framing, template block and clamped params verbatim.
    """
    await _studio_flag_gate(user)
    template = await db.get(Template, session.template_id) if session.template_id else None
    try:
        plan = await resolve_session_call(template, session.params)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    block = template_prompt_block(template)
    persona_only = chat_service.chat_system_prompt(user.persona, "")
    segments = [PromptSegment(label="Persona framing", content=persona_only)]
    if block:
        segments.append(PromptSegment(label="Template guardrail block", content=block.strip()))
    substrate_block = chat_service.substrate_prompt_block(session)
    if substrate_block:
        # Brownfield substrate as its own labeled segment (spec R2.4)
        segments.append(
            PromptSegment(label="Brownfield substrate", content=substrate_block.strip())
        )
    from app.services import connectors as connectors_svc

    connectors_block = await connectors_svc.connector_grounding_block(db)
    if connectors_block:
        # C2 grounding — the transparency rail shows exactly what rides along
        segments.append(
            PromptSegment(label="Registered connectors", content=connectors_block.strip())
        )
    params_desc = f"max_tokens={plan.max_tokens}"
    if plan.temperature is not None:
        params_desc += f", temperature={plan.temperature}"
    segments.append(
        PromptSegment(
            label="Clamped parameters (applied at call time, not prompt text)",
            content=params_desc,
        )
    )
    return PromptContextOut(
        segments=segments, model_id=plan.model_id, params=plan.effective
    )


@router.get("/sessions/{session_id}/effective-params", response_model=EffectiveParamsOut)
async def effective_params(
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> EffectiveParamsOut:
    """Requested vs effective from the SAME resolve/clamp path the invocation
    seam uses (studio spec R3.2) — display cannot drift from enforcement."""
    await _studio_flag_gate(user)
    template = await db.get(Template, session.template_id) if session.template_id else None
    try:
        plan = await resolve_session_call(template, session.params)
        models = await resolve_models(template)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return EffectiveParamsOut(
        requested=plan.requested,
        effective=plan.effective,
        clamped_by=plan.clamped_by,
        allowed_models=list(models.allowed),
        template_name=models.template_name,
    )


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    # Destructive on a transcript: creator or project OWNER only (not editors)
    if session.user_id != user.id:
        allowed = False
        if session.project_id is not None:
            from app.services import collab

            resolved = await collab.resolve_role(db, session.project_id, user)
            allowed = resolved is not None and resolved[1] == "owner"
        if not allowed:
            raise HTTPException(
                status_code=403, detail="Only the session creator or project owner may delete it"
            )
    session_id = session.id
    # 13.5AB: detach provenance rows explicitly (the DB also does this via
    # ON DELETE SET NULL after migration a0b1c2d3e4f5 — belt and braces, and
    # the only enforcement on SQLite test engines). Spec versions and
    # generation history stay with the PROJECT; only the pointer to the
    # deleted transcript goes.
    from sqlalchemy import update

    from app.models import Spec, SpecGeneration

    await db.execute(
        update(Spec).where(Spec.session_id == session_id).values(session_id=None)
    )
    await db.execute(
        update(SpecGeneration)
        .where(SpecGeneration.session_id == session_id)
        .values(session_id=None)
    )
    # Row FIRST, transcript SECOND. The old order wiped DynamoDB and then hit
    # the FK violation, leaving a headless session that rendered empty. An
    # orphaned transcript is invisible and reapable; a zombie row is not.
    await db.delete(session)
    await db.commit()
    try:
        await chat_service.delete_messages(session_id)
    except Exception:  # noqa: BLE001 — the user-visible delete already succeeded
        logger.exception(
            "Transcript cleanup failed for deleted session %s (orphaned items are reapable)",
            session_id,
        )


@router.post("/sessions/{session_id}/messages")
async def send_message(
    payload: MessageIn,
    request: Request,
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream the assistant reply over SSE; template model guardrails apply (S2-06)."""
    template = await db.get(Template, session.template_id) if session.template_id else None
    try:
        # Session-requested overrides (studio rail) through the shared clamp
        # path — sessions without params get exactly the pre-S18 defaults.
        plan = await resolve_session_call(template, session.params)
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Resolve native guardrail availability before the SSE response opens so
    # missing fail-closed configuration uses the normal HTTP 503 handler. An
    # OpenAI-compatible endpoint has no Bedrock guardrail dependency; its
    # adapter applies the same trusted policy through in-process redaction.
    if not plan.model_id.startswith("ext/"):
        from app.services.bedrock import guardrail_config

        await guardrail_config(streaming=True, purpose="chat")

    # Rate-limit + cost-cap preflight BEFORE the SSE stream opens, so refusals
    # are real 429s, not error events inside a 200 stream (S5 R2/R5.3).
    await preflight_model_call(
        InvocationCtx(
            purpose="chat", user_id=user.id,
            project_id=session.project_id, session_id=session.id,
        ),
        streaming=True,
    )
    # S16-04; S18: `surface` detail distinguishes studio adoption, no new event types.
    surface = request.headers.get("x-marshal-surface")
    analytics.track(
        user.id, "chat_message", project_id=session.project_id,
        detail={"surface": surface} if surface == "studio" else None,
    )

    history = await chat_service.get_messages(session.id)
    if not history:
        session.title = chat_service.derive_title(payload.content)
    session.status = "chatting"
    session.model_id = plan.model_id
    await db.commit()

    await chat_service.put_message(session.id, "user", payload.content)
    history.append({"role": "user", "content": payload.content})
    messages = chat_service.build_converse_messages(history)
    from app.services import connectors as connectors_svc

    system = chat_service.chat_system_prompt(
        user.persona,
        template_prompt_block(template),
        chat_service.substrate_prompt_block(session),
        await connectors_svc.connector_grounding_block(db),  # C2, flag-gated
    )
    model_id = plan.model_id

    async def event_stream():
        result = StreamResult()
        try:
            async for delta in stream_converse(
                messages=messages,
                system=system,
                model_id=model_id,
                max_tokens=plan.max_tokens,
                temperature=plan.temperature,
                result=result,
                ctx=InvocationCtx(
                    purpose="chat",
                    user_id=user.id,
                    project_id=session.project_id,
                    session_id=session.id,
                ),
                enforce=False,  # preflighted above, pre-stream
            ):
                yield {"event": "delta", "data": json.dumps({"text": delta})}
            await chat_service.put_message(
                session.id,
                "assistant",
                result.text,
                model_id=model_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            yield {
                "event": "done",
                "data": json.dumps(
                    {
                        "model_id": model_id,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "latency_ms": result.latency_ms,
                    }
                ),
            }
        except Exception as exc:
            logger.exception("Stream failed for session %s", session.id)
            if result.text:
                await chat_service.put_message(
                    session.id, "assistant", result.text, model_id=model_id, truncated=True
                )
            code = type(exc).__name__
            if "Throttling" in code:
                message = (
                    "Generating is taking longer than usual and the request was throttled. "
                    "Please retry."
                )
            elif isinstance(exc, ReadTimeoutError) and result.text:
                # Mid-stream stall: the stream client's read timeout fired after
                # tokens had already flowed (no retry past the first token), so
                # the partial reply above is what the user keeps.
                message = STREAM_STALL_MESSAGE
            else:
                message = "The model request failed. Please try again."
            yield {"event": "error", "data": json.dumps({"code": code, "message": message})}

    return EventSourceResponse(event_stream())


# ------------------------------------------------------------------ generation


@router.post("/sessions/{session_id}/generate-spec", response_model=GenerationOut, status_code=202)
async def generate_spec(
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GenerationOut:
    if session.mode == "guided":
        # Persist the wizard brief as the transcript the pipeline reads (R3.1)
        try:
            await guided_service.prepare_generation(db, session, user)
        except guided_service.GuidedStateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _start_generation(db, user, session, None)


@router.post(
    "/sessions/{session_id}/generate-spec/{doc_type}",
    response_model=GenerationOut,
    status_code=202,
)
async def regenerate_doc(
    doc_type: str,
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GenerationOut:
    if doc_type not in DOC_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown document type: {doc_type}")
    return await _start_generation(db, user, session, doc_type)


async def _start_generation(
    db: AsyncSession, user: User, session: ChatSession, only_type: str | None
) -> GenerationOut:
    try:
        generation = await specgen.start_generation(db, user, session, only_type)
    except specgen.GenerationInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GuardrailViolation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return GenerationOut.model_validate(generation)


@router.get("/sessions/{session_id}/generate-spec/latest", response_model=GenerationOut | None)
async def latest_generation(
    session: ChatSession = Depends(_owned_session), db: AsyncSession = Depends(get_db)
) -> GenerationOut | None:
    generation = await specgen.get_latest_generation(db, session.id)
    return GenerationOut.model_validate(generation) if generation else None


@router.get("/sessions/{session_id}/generate-spec/stream")
async def stream_generation(
    session: ChatSession = Depends(_owned_session), db: AsyncSession = Depends(get_db)
):
    generation = await specgen.get_latest_generation(db, session.id)
    if generation is None:
        raise HTTPException(status_code=404, detail="No generation for this session")
    generation_id = str(generation.id)
    snapshot = {"docs": generation.docs, "status": generation.status}
    is_terminal = generation.status != "running"

    async def event_stream():
        yield {"event": "snapshot", "data": json.dumps(snapshot)}
        if is_terminal:
            yield {"event": "done", "data": json.dumps({"status": snapshot["status"]})}
            return
        queue = await specgen.generation_bus.subscribe(generation_id)
        try:
            while True:
                event = await queue.get()
                if event.get("type") == "done":
                    yield {"event": "done", "data": json.dumps(event)}
                    return
                yield {"event": event.get("type", "update"), "data": json.dumps(event)}
        finally:
            await specgen.generation_bus.unsubscribe(generation_id, queue)

    return EventSourceResponse(event_stream())


# ------------------------------------------------------------------ spec reads


async def _doc_state(db: AsyncSession, project_id: uuid.UUID, doc_type: str) -> DocSpecState:
    result = await db.execute(
        select(Spec)
        .where(Spec.project_id == project_id, Spec.type == doc_type)
        .order_by(Spec.version.desc())
    )
    specs = list(result.scalars())
    if not specs:
        return DocSpecState()
    return DocSpecState(
        latest=SpecOut.model_validate(specs[0]),
        versions=[SpecVersionInfo.model_validate(s) for s in specs],
    )


@router.get("/sessions/{session_id}/spec", response_model=FullSpecResponse)
async def get_session_spec(
    session: ChatSession = Depends(_owned_session), db: AsyncSession = Depends(get_db)
) -> FullSpecResponse:
    empty = FullSpecResponse(
        requirements=DocSpecState(), design=DocSpecState(), tasks=DocSpecState()
    )
    if session.project_id is None:
        return empty
    return FullSpecResponse(
        requirements=await _doc_state(db, session.project_id, "requirements"),
        design=await _doc_state(db, session.project_id, "design"),
        tasks=await _doc_state(db, session.project_id, "tasks"),
    )


@router.post("/sessions/{session_id}/spec/save", response_model=FullSpecResponse)
async def save_spec(
    request: Request,
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> FullSpecResponse:
    session.status = "saved"
    if session.mode == "guided" and (session.guided_state or {}).get("step") == "generate":
        session.guided_state = guided_service.mark_done(session)  # Approve & Save → done (R3.3)
    await db.commit()
    surface = request.headers.get("x-marshal-surface")
    analytics.track(
        session.user_id, "spec_saved", project_id=session.project_id,  # S16-04
        detail={"surface": surface} if surface == "studio" else None,
    )
    return await get_session_spec(session, db)


@router.get("/specs/{spec_id}/download")
async def download_spec(
    spec_id: uuid.UUID,
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    spec = (await db.execute(select(Spec).where(Spec.id == spec_id))).scalar_one_or_none()
    if spec is None:
        raise HTTPException(status_code=404, detail="Spec not found")
    from app.services import collab

    if await collab.resolve_role(db, spec.project_id, user) is None:  # viewer+ (R1.3)
        raise HTTPException(status_code=404, detail="Spec not found")
    return PlainTextResponse(
        spec.content,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{spec.type}-v{spec.version}.md"'
        },
    )


# ------------------------------------------------------------------ guided mode (S4-05)


@router.post("/sessions/{session_id}/guided/answer", response_model=SessionOut)
async def guided_answer(
    payload: GuidedAnswerIn,
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Submit (or revise via back-nav) a form step; state persists server-side."""
    try:
        session.guided_state = guided_service.advance_step(
            session, payload.step, payload.answers
        )
    except guided_service.GuidedStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/sessions/{session_id}/guided/clarify", response_model=SessionOut)
async def guided_clarify(
    payload: GuidedClarifyIn,
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """Record answers for outstanding questions, then run the next round (or proceed)."""
    try:
        if payload.answers:
            session.guided_state = guided_service.record_clarify_answers(
                session, payload.answers
            )
            await db.commit()
            await db.refresh(session)
        await guided_service.run_clarify_round(db, session, user)
    except guided_service.GuidedStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.refresh(session)
    return session


@router.post("/sessions/{session_id}/guided/proceed", response_model=SessionOut)
async def guided_proceed(
    session: ChatSession = Depends(_owned_session),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    """User elects to proceed with assumptions (R2.2)."""
    try:
        await guided_service.freeze_assumptions(db, session, reason="user_proceeded")
    except guided_service.GuidedStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.refresh(session)
    return session


@router.post("/sessions/{session_id}/guided/switch-freeform", response_model=SessionOut)
async def guided_switch_freeform(
    session: ChatSession = Depends(_owned_session),
    user: User = Depends(require_admin_security_if_admin),
    db: AsyncSession = Depends(get_db),
) -> ChatSession:
    try:
        return await guided_service.switch_to_freeform(db, session, user)
    except guided_service.GuidedStateError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
