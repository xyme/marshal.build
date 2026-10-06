"""Bedrock runtime wrapper: streaming + non-streaming converse with retries.

boto3 is synchronous; streaming runs in a thread feeding an asyncio queue so the
event loop stays responsive (ai-chat-spec-gen design.md).
"""

import asyncio
import hashlib
import logging
import random
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectionError,
    ReadTimeoutError,
)

from app.core.config import get_settings

logger = logging.getLogger("marshal.bedrock")


@dataclass(frozen=True)
class InvocationCtx:
    """Attribution for model-usage logging (audit-logging spec R2).

    Every caller threads one through; the seam below records the invocation
    regardless of outcome. Fields are optional because system flows (e.g.
    rehydration) may lack a user.
    """

    purpose: str  # chat|requirements|design|tasks|title|classification
    user_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    generation_id: uuid.UUID | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _prompt_text(messages: list[dict], system: str) -> str:
    parts = [f"[system]\n{system}"] if system else []
    for message in messages:
        content = "".join(block.get("text", "") for block in message.get("content", []))
        parts.append(f"[{message.get('role', '?')}]\n{content}")
    return "\n\n".join(parts)


async def _record_invocation(
    ctx: InvocationCtx,
    *,
    model_id: str,
    prompt: str,
    response_text: str,
    input_tokens: int,
    output_tokens: int,
    stop_reason: str | None,
    latency_ms: int,
    success: bool,
    error_class: str | None = None,
    usage_estimated: bool = False,
) -> None:
    """Fire-and-forget write of a model_invocations row. Never raises."""
    try:
        from app.core.db import SessionLocal
        from app.models import ModelInvocation
        from app.services.pricing import compute_cost_ext, compute_cost_usd

        if not success:
            cost = None
        elif model_id.startswith("ext/"):
            # S17-05: external calls price from the admin-entered rates —
            # required at registration, so "unpriced" is unreachable here
            # short of the endpoint row vanishing mid-flight.
            cost = await compute_cost_ext(model_id, input_tokens, output_tokens)
        else:
            cost = compute_cost_usd(model_id, input_tokens, output_tokens)
        if success and cost is None:
            logger.warning("no pricing for model %s — cost logged as NULL", model_id)
        async with SessionLocal() as session:
            session.add(
                ModelInvocation(
                    user_id=ctx.user_id,
                    project_id=ctx.project_id,
                    session_id=ctx.session_id,
                    generation_id=ctx.generation_id,
                    purpose=ctx.purpose,
                    model_id=model_id,
                    prompt_text=prompt,
                    prompt_sha256=_sha256(prompt),
                    response_text=response_text,
                    response_sha256=_sha256(response_text) if response_text else None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    stop_reason=stop_reason,
                    latency_ms=latency_ms,
                    cost_usd=cost,
                    success=success,
                    error_class=error_class,
                    usage_estimated=usage_estimated,
                )
            )
            await session.commit()
            # Real-time spend accounting + threshold alerts (S5 R1/R3)
            if cost:
                from decimal import Decimal

                from app.services import spend as spend_svc

                caps = await spend_svc.resolve_caps(session, ctx.user_id, ctx.project_id)
                crossings = await spend_svc.TRACKER.add(
                    ctx.user_id, ctx.project_id, Decimal(str(cost)), caps
                )
                if crossings:
                    await spend_svc.record_crossings(session, crossings)
    except Exception:  # noqa: BLE001 — logging must never break the call path
        logger.exception("model invocation log failed (purpose=%s)", ctx.purpose)


async def preflight_model_call(ctx: InvocationCtx, *, streaming: bool = False) -> None:
    """Security-snapshot, rate-limit, and cost-cap gate before a model call.

    Raises SecuritySettingsUnavailable (503), RateLimited (429 + Retry-After),
    or CostCapExceeded (429 cost_cap). SSE paths use a 5s max wait (fast-fail
    pre-stream); others queue ≤30s per FSD §4.6.7. Non-security accounting
    checks retain their documented fail-open behavior.
    """
    from app.core.db import SessionLocal
    from app.services import spend as spend_svc
    from app.services.platform_settings import get_security_controls
    from app.services.ratelimit import BUCKETS, RateLimited

    # Establish a trustworthy redaction policy before rate/cost side effects
    # and, for chat, before the SSE response is opened.
    await get_security_controls()

    try:
        await BUCKETS.acquire(
            ctx.user_id, ctx.project_id, max_wait_s=5.0 if streaming else 30.0
        )
    except RateLimited as exc:
        # Rate-limit refusals are security events (FSD §4.6.1)
        from app.services import audit

        audit.emit(
            audit.write_entry(
                actor_id=ctx.user_id, category=audit.SECURITY, action="rate_limited",
                detail={"scope": exc.scope, "retry_after_s": exc.retry_after_s,
                        "purpose": ctx.purpose},
                http_status=429,
            )
        )
        raise
    try:
        async with SessionLocal() as session:
            caps = await spend_svc.resolve_caps(session, ctx.user_id, ctx.project_id)
        await spend_svc.TRACKER.check(
            ctx.user_id, ctx.project_id, caps,
            exempt_user_scopes=ctx.purpose in spend_svc.CAP_EXEMPT_PURPOSES,
        )
    except spend_svc.CostCapExceeded:
        raise
    except Exception:  # noqa: BLE001 — fail open (R1.3)
        logger.exception("cap preflight failed; proceeding (fail open)")


def _emit(coro) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # pragma: no cover — scripts without a loop
        asyncio.run(coro)

# Bedrock's documented transient classes. InternalServerException added 13.5Z:
# a 5xx "unexpected error … try your request again" killed a multi-call
# codegen build outright (zero retries) minutes after a ServiceUnavailable
# exhausted its three — the same outage, two codes, one of them unhandled.
RETRYABLE = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
}
# Transport-level failures (connection closed mid-request, endpoint unreachable,
# read timeout) are retryable the same way — observed live during S8 codegen
# (multi-call builds raise the exposure to transient socket death).
RETRYABLE_CONNECTION_ERRORS = (ConnectionClosedError, ConnectionError, ReadTimeoutError)
MAX_ATTEMPTS = 3
# Streaming first-token budget. A Bedrock stall (2026-10) froze chat for ~3 min
# because the stream shared the 180 s non-streaming read timeout; the retry
# path in stream_converse only runs BEFORE the first token, so a short read
# timeout on the stream client lets that retry fire within ~45 s. Once tokens
# flow, inter-event gaps are far below this, so the same timeout also catches
# a model that stops responding mid-stream (surfaced as an error, never a hang).
FIRST_TOKEN_TIMEOUT_S = 45

_client = None
_stream_client = None
_client_lock = threading.Lock()


def bedrock_client():
    """Non-streaming client: codegen/specgen `converse` calls legitimately
    run 60 s+ and keep the long read timeout."""
    global _client
    with _client_lock:
        if _client is None:
            _client = boto3.client(
                "bedrock-runtime",
                region_name=get_settings().aws_region,
                config=Config(read_timeout=180, retries={"max_attempts": 0}),
            )
        return _client


def bedrock_stream_client():
    """Streaming client: short read timeout so a stalled stream fails fast
    (FIRST_TOKEN_TIMEOUT_S); retry policy stays in stream_converse."""
    global _stream_client
    with _client_lock:
        if _stream_client is None:
            _stream_client = boto3.client(
                "bedrock-runtime",
                region_name=get_settings().aws_region,
                config=Config(
                    read_timeout=FIRST_TOKEN_TIMEOUT_S,
                    connect_timeout=10,
                    retries={"max_attempts": 0},
                ),
            )
        return _stream_client


@dataclass
class StreamResult:
    """Mutable holder filled in as the stream progresses."""

    text_parts: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


def _backoff(attempt: int) -> float:
    return min(2**attempt + random.random(), 8.0)


async def _inference_config(
    max_tokens: int, temperature: float | None, top_p: float | None
) -> dict:
    """Clamp every call to the platform parameter bounds (S4-04 R4.3).

    This is the single choke point: no caller can exceed the admin-configured
    max_tokens, and any temperature/top_p a future surface passes is clamped
    into the platform window before it reaches Bedrock.
    """
    from app.services.platform_settings import clamp_temperature, clamp_top_p, get_controls

    controls = await get_controls()
    config: dict = {"maxTokens": min(max_tokens, controls.max_tokens)}
    temperature = clamp_temperature(temperature, controls)
    top_p = clamp_top_p(top_p, controls)
    if temperature is not None:
        # Several Converse families reject temperature and top-p together even
        # though both are common fields. Prefer the explicitly requested
        # temperature; use top-p only when temperature is absent.
        config["temperature"] = temperature
    elif top_p is not None:
        config["topP"] = top_p
    return config


def _optional_inference_config_rejected(exc: ClientError, config: dict) -> bool:
    """Whether a family rejected an optional common inference parameter.

    Retrying with maxTokens only is safe: ValidationException happens before
    billable inference, and security/system/guardrail fields remain untouched.

    "deprecated" joined the rejection markers 5 Oct 2026: Claude Sonnet 5
    answers "`temperature` is deprecated for this model" — the same optional
    field refused with new wording. Without the marker the call failed instead
    of retrying, so risk scoring (temperature 0 on the fast model) errored on
    every sonnet-5-only template and the deploy gate returned 403 risk_error.
    A deprecated MODEL ("model X is deprecated") names no optional field and
    is still raised untouched.
    """
    if len(config) == 1:
        return False
    error = exc.response.get("Error", {})
    if error.get("Code") != "ValidationException":
        return False
    message = str(error.get("Message", "")).lower()
    names_optional_field = any(
        marker in message
        for marker in (
            "temperature",
            "top_p",
            "topp",
            "top p",
            "top_k",
            "topk",
            "inferenceconfig",
        )
    )
    return names_optional_field and any(
        marker in message
        for marker in (
            "not supported",
            "unsupported",
            "extraneous",
            "not allowed",
            "deprecated",
        )
    )


def _optional_inference_params(config: dict) -> list[str]:
    """Names of the fields a maxTokens-only retry drops (for the WARNING log)."""
    return sorted(key for key in config if key != "maxTokens")


def additional_request_fields(model_id: str) -> dict:
    """Model-family request extras — currently: suppress adaptive reasoning.

    Claude models with adaptive thinking emit `reasoningContent` blocks by
    DEFAULT, and those tokens are billed outputTokens. Both seam paths extract
    text blocks only and no marshal surface renders reasoning, so the spend is
    invisible waste — and fixed-budget calls starve. Proven live 18 Sep 2026
    (13.5Y): sonnet-5 codegen plan calls burned their whole 2000-token budget
    on reasoning and returned ZERO text; template assembly filled 8192 tokens
    holding ~3k tokens of JSON. The codegen runner's HeadlessBedrock mirrors
    this policy (same generation code, same exposure).
    """
    if "anthropic.claude" in model_id:
        return {"thinking": {"type": "disabled"}}
    return {}


def _thinking_field_rejected(exc: ClientError, fields: dict) -> bool:
    """Whether the model rejected the thinking-suppression request field.

    Families that predate adaptive thinking may refuse the field outright.
    Dropping it and retrying is safe: such models never emitted reasoning in
    the first place, and ValidationException precedes billable inference.
    """
    if not fields:
        return False
    error = exc.response.get("Error", {})
    if error.get("Code") != "ValidationException":
        return False
    message = str(error.get("Message", "")).lower()
    return (
        "thinking" in message
        or "additionalmodelrequestfields" in message
        or "additional model request fields" in message
    )


def _transient_invalid_model(exc: ClientError) -> bool:
    """Cross-region inference profiles intermittently answer 'The provided
    model identifier is invalid' for an identifier that succeeds seconds
    earlier and later on the same client (observed live 18 Sep 2026, 13.5Y:
    an s8 build died on file 2/3 after two clean calls; a 16-call hammer on
    the same container could not reproduce). Treat it like the other observed
    transients: a genuinely wrong identifier just fails MAX_ATTEMPTS fast
    ValidationExceptions with bounded backoff — no billable inference happens.
    """
    error = exc.response.get("Error", {})
    return (
        error.get("Code") == "ValidationException"
        and "model identifier is invalid" in str(error.get("Message", "")).lower()
    )


async def guardrail_config(streaming: bool, purpose: str | None = None) -> dict:
    """`guardrailConfig` kwargs for native Converse calls.

    An empty mapping is valid only when trusted controls do not require
    redaction for this purpose. If redaction is required, missing native
    guardrail configuration fails through the existing security-settings 503
    path instead of allowing an unguarded Bedrock request.
    """
    from app.services.platform_settings import (
        SecuritySettingsUnavailable,
        redaction_active,
    )

    if not await redaction_active(purpose):
        return {}
    settings = get_settings()
    if not settings.bedrock_guardrail_id:
        if settings.security_fail_closed:
            raise SecuritySettingsUnavailable(
                "PII redaction requires a configured Bedrock guardrail"
            )
        # Local/dev retains the historical bootstrap posture. Cloud explicitly
        # pins SECURITY_FAIL_CLOSED=true, so this branch cannot send an
        # unguarded request in the private-beta installation.
        return {}
    config: dict = {
        "guardrailIdentifier": settings.bedrock_guardrail_id,
        "guardrailVersion": settings.bedrock_guardrail_version or "DRAFT",
    }
    if streaming:
        # Async mode would let un-redacted tokens reach the client before the
        # guardrail verdict; synchronous keeps redaction ahead of the stream.
        config["streamProcessingMode"] = "sync"
    return {"guardrailConfig": config}


async def stream_converse(
    *,
    messages: list[dict],
    system: str,
    model_id: str,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    result: StreamResult | None = None,
    ctx: InvocationCtx | None = None,
    enforce: bool = True,
) -> AsyncGenerator[str, None]:
    """Yield text deltas from converse_stream. Retries only before first token.

    When `ctx` is provided the completed (or failed) stream is recorded to
    model_invocations after accumulation (audit-logging spec R2.5).
    `enforce=False` skips the rate/cap preflight — for callers that already
    preflighted explicitly (chat does, pre-SSE, to fail with a real 429).
    """
    if model_id.startswith("ext/"):
        # S17-04: OpenAI-compatible endpoints. The adapter honors this exact
        # contract (preflight, clamps, recording) and FAILS CLOSED [D16].
        from app.services.model_providers import openai_compat

        async for delta in openai_compat.stream_converse_ext(
            messages=messages, system=system, model_id=model_id,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            result=result, ctx=ctx, enforce=enforce,
        ):
            yield delta
        return

    settings = get_settings()
    if ctx is not None and enforce:
        await preflight_model_call(ctx, streaming=True)
    max_tokens = max_tokens or settings.bedrock_max_tokens
    inference_config = await _inference_config(max_tokens, temperature, top_p)
    guardrail = await guardrail_config(
        streaming=True, purpose=ctx.purpose if ctx else None
    )  # S14-04
    result = result if result is not None else StreamResult()
    started = time.monotonic()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()
    _SENTINEL = object()

    def producer() -> None:
        first_token_sent = False
        request_config = dict(inference_config)
        request_fields = additional_request_fields(model_id)
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = bedrock_stream_client().converse_stream(
                    modelId=model_id,
                    messages=messages,
                    system=[{"text": system}],
                    inferenceConfig=request_config,
                    **(
                        {"additionalModelRequestFields": request_fields}
                        if request_fields
                        else {}
                    ),
                    **guardrail,
                )
                for event in response["stream"]:
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"]["delta"].get("text", "")
                        if delta:
                            first_token_sent = True
                            result.text_parts.append(delta)
                            asyncio.run_coroutine_threadsafe(queue.put(delta), loop).result()
                    elif "messageStop" in event:
                        result.stop_reason = event["messageStop"].get("stopReason")
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        result.input_tokens = usage.get("inputTokens", 0)
                        result.output_tokens = usage.get("outputTokens", 0)
                        result.latency_ms = event["metadata"].get("metrics", {}).get(
                            "latencyMs", 0
                        )
                asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop).result()
                return
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if (
                    not first_token_sent
                    and attempt < MAX_ATTEMPTS - 1
                    and _thinking_field_rejected(exc, request_fields)
                ):
                    logger.warning(
                        "Bedrock model %s rejected thinking suppression; "
                        "retrying without additional request fields",
                        model_id,
                    )
                    request_fields = {}
                    continue
                if (
                    not first_token_sent
                    and attempt < MAX_ATTEMPTS - 1
                    and _optional_inference_config_rejected(exc, request_config)
                ):
                    logger.warning(
                        "Bedrock model %s rejected optional inference parameters %s; "
                        "retrying with maxTokens only",
                        model_id, _optional_inference_params(request_config),
                    )
                    request_config = {"maxTokens": request_config["maxTokens"]}
                    continue
                if (
                    not first_token_sent
                    and attempt < MAX_ATTEMPTS - 1
                    and _transient_invalid_model(exc)
                ):
                    logger.warning(
                        "Bedrock transient invalid-model response for %s, retry %d",
                        model_id, attempt + 1,
                    )
                    threading.Event().wait(_backoff(attempt))
                    continue
                if code in RETRYABLE and not first_token_sent and attempt < MAX_ATTEMPTS - 1:
                    logger.warning("Bedrock %s, retry %d", code, attempt + 1)
                    threading.Event().wait(_backoff(attempt))
                    continue
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
                return
            except RETRYABLE_CONNECTION_ERRORS as exc:
                if not first_token_sent and attempt < MAX_ATTEMPTS - 1:
                    logger.warning("Bedrock connection error, retry %d: %s", attempt + 1, exc)
                    threading.Event().wait(_backoff(attempt))
                    continue
                if first_token_sent:
                    # No retry after the first token (the client already has
                    # partial text); the consumer records the partial result.
                    logger.error(
                        "Bedrock model %s stopped responding mid-stream after %d chars: %s",
                        model_id, len(result.text), exc,
                    )
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
                return
            except Exception as exc:  # pragma: no cover
                asyncio.run_coroutine_threadsafe(queue.put(exc), loop).result()
                return

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    except Exception as exc:
        if ctx is not None:
            _emit(
                _record_invocation(
                    ctx,
                    model_id=model_id,
                    prompt=_prompt_text(messages, system),
                    response_text=result.text,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    stop_reason=result.stop_reason,
                    latency_ms=result.latency_ms or int((time.monotonic() - started) * 1000),
                    success=False,
                    error_class=type(exc).__name__,
                )
            )
        raise
    if ctx is not None:
        _emit(
            _record_invocation(
                ctx,
                model_id=model_id,
                prompt=_prompt_text(messages, system),
                response_text=result.text,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                stop_reason=result.stop_reason,
                latency_ms=result.latency_ms or int((time.monotonic() - started) * 1000),
                success=True,
            )
        )


async def converse(
    *,
    messages: list[dict],
    system: str,
    model_id: str,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    ctx: InvocationCtx | None = None,
    enforce: bool = True,
) -> tuple[str, dict, str | None]:
    """Non-streaming converse with retry. Returns (text, usage, stop_reason).

    When `ctx` is provided the call (success or failure) is recorded to
    model_invocations (audit-logging spec R2).
    """
    if model_id.startswith("ext/"):
        from app.services.model_providers import openai_compat  # S17-04

        return await openai_compat.converse_ext(
            messages=messages, system=system, model_id=model_id,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            ctx=ctx, enforce=enforce,
        )

    settings = get_settings()
    if ctx is not None and enforce:
        await preflight_model_call(ctx, streaming=False)
    max_tokens = max_tokens or settings.bedrock_max_tokens
    inference_config = await _inference_config(max_tokens, temperature, top_p)
    guardrail = await guardrail_config(
        streaming=False, purpose=ctx.purpose if ctx else None
    )  # S14-04
    started = time.monotonic()

    def call() -> tuple[str, dict, str | None]:
        last_exc: Exception | None = None
        request_config = dict(inference_config)
        request_fields = additional_request_fields(model_id)
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = bedrock_client().converse(
                    modelId=model_id,
                    messages=messages,
                    system=[{"text": system}],
                    inferenceConfig=request_config,
                    **(
                        {"additionalModelRequestFields": request_fields}
                        if request_fields
                        else {}
                    ),
                    **guardrail,
                )
                text = "".join(
                    block.get("text", "")
                    for block in resp["output"]["message"]["content"]
                )
                return text, resp.get("usage", {}), resp.get("stopReason")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                last_exc = exc
                if (
                    attempt < MAX_ATTEMPTS - 1
                    and _thinking_field_rejected(exc, request_fields)
                ):
                    logger.warning(
                        "Bedrock model %s rejected thinking suppression; "
                        "retrying without additional request fields",
                        model_id,
                    )
                    request_fields = {}
                    continue
                if (
                    attempt < MAX_ATTEMPTS - 1
                    and _optional_inference_config_rejected(exc, request_config)
                ):
                    logger.warning(
                        "Bedrock model %s rejected optional inference parameters %s; "
                        "retrying with maxTokens only",
                        model_id, _optional_inference_params(request_config),
                    )
                    request_config = {"maxTokens": request_config["maxTokens"]}
                    continue
                if attempt < MAX_ATTEMPTS - 1 and _transient_invalid_model(exc):
                    logger.warning(
                        "Bedrock transient invalid-model response for %s, retry %d",
                        model_id, attempt + 1,
                    )
                    threading.Event().wait(_backoff(attempt))
                    continue
                if code in RETRYABLE and attempt < MAX_ATTEMPTS - 1:
                    threading.Event().wait(_backoff(attempt))
                    continue
                raise
            except RETRYABLE_CONNECTION_ERRORS as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS - 1:
                    logger.warning("Bedrock connection error, retry %d: %s", attempt + 1, exc)
                    threading.Event().wait(_backoff(attempt))
                    continue
                raise
        raise last_exc  # pragma: no cover

    try:
        text, usage, stop_reason = await asyncio.to_thread(call)
    except Exception as exc:
        if ctx is not None:
            _emit(
                _record_invocation(
                    ctx,
                    model_id=model_id,
                    prompt=_prompt_text(messages, system),
                    response_text="",
                    input_tokens=0,
                    output_tokens=0,
                    stop_reason=None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False,
                    error_class=type(exc).__name__,
                )
            )
        raise
    if ctx is not None:
        _emit(
            _record_invocation(
                ctx,
                model_id=model_id,
                prompt=_prompt_text(messages, system),
                response_text=text,
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                stop_reason=stop_reason,
                latency_ms=int((time.monotonic() - started) * 1000),
                success=True,
            )
        )
    return text, usage, stop_reason
