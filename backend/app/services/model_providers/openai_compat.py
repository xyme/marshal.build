"""OpenAI-compatible chat-completions adapter (S17-04, decision D13).

One adapter, one contract: `POST {base_url}/chat/completions` in streaming and
non-streaming modes — the shape vLLM, Ollama, TGI, LM Studio and most
enterprise gateways expose. The bedrock module dispatches here for `ext/*`
model ids; this module honors the SAME seam contract (preflight, clamps,
invocation recording) and adds the external-only obligations:

  - pre-egress redaction when `security.pii_redaction` is on (S17-05 —
    Bedrock's managed guardrail cannot cover calls that leave AWS);
  - usage estimation (flagged) when the endpoint omits token counts;
  - FAIL CLOSED [D16]: endpoint failure is a structured, named error —
    never a silent retry against Bedrock.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

import httpx

logger = logging.getLogger("marshal.openai_compat")

MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# OpenAI finish_reason → platform stop_reason
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens"}


class ExternalEndpointError(Exception):
    """External endpoint failure — fail closed [D16]; surfaces as 502.

    Carries enough for the S15-05 copy bar: which endpoint, what happened,
    where an admin fixes it.
    """

    def __init__(self, label: str, slug: str, detail: str):
        self.label = label
        self.slug = slug
        self.detail = detail
        super().__init__(
            f'Custom model endpoint "{label}" failed: {detail} — an administrator '
            "can test or update it under Admin → Model Controls → Custom endpoints."
        )


def _estimate_tokens(text: str) -> int:
    """chars/4 heuristic — flagged on the row via usage_estimated, never silent."""
    return max(1, (len(text) + 3) // 4)


def _to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    """Converse shape → chat-completions shape."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for message in messages:
        text = "".join(
            block.get("text", "") for block in message.get("content", [])
        )
        out.append({"role": message.get("role", "user"), "content": text})
    return out


async def _prepare(
    model_id: str, messages: list[dict], system: str, purpose: str | None = None
):
    """Shared call setup: endpoint config, redaction, headers, payload base."""
    from app.services import model_endpoints

    slug = model_id.removeprefix("ext/")
    try:
        cfg = await model_endpoints.endpoint_config(slug)
    except model_endpoints.EndpointNotFound as exc:
        raise ExternalEndpointError(slug, slug, "endpoint is not configured") from exc
    if not cfg.get("enabled", True):
        raise ExternalEndpointError(
            cfg.get("label", slug), slug, "endpoint is disabled"
        )

    redaction_counts: dict[str, int] = {}
    from app.services.platform_settings import redaction_active

    # Resolve the trusted policy before constructing any outbound request.
    # In cloud fail-closed mode, an unavailable snapshot must never become an
    # unredacted external-model call.
    pii_on = await redaction_active(purpose)
    if pii_on:
        from app.services.redaction import redact_messages

        started = time.perf_counter()
        messages, system, redaction_counts = redact_messages(messages, system)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if redaction_counts:
            logger.info(
                "pre-egress redaction on %s: %s (%.2fms)",
                slug, redaction_counts, elapsed_ms,
            )

    headers = {"Content-Type": "application/json"}
    key = await model_endpoints.api_key_for(slug)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return cfg, slug, messages, system, headers


def _payload(cfg: dict, messages: list[dict], system: str, inference: dict) -> dict:
    body: dict = {
        "model": cfg["model_name"],
        "messages": _to_openai_messages(messages, system),
        "max_tokens": inference.get("maxTokens"),
    }
    if "temperature" in inference:
        body["temperature"] = inference["temperature"]
    if "topP" in inference:
        body["top_p"] = inference["topP"]
    return body


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                            httpx.RemoteProtocolError, httpx.PoolTimeout))


async def converse_ext(
    *,
    messages: list[dict],
    system: str,
    model_id: str,
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None,
    ctx,
    enforce: bool,
) -> tuple[str, dict, str | None]:
    """Non-streaming external call mirroring bedrock.converse's contract."""
    from app.services import bedrock

    if ctx is not None and enforce:
        await bedrock.preflight_model_call(ctx, streaming=False)
    settings_max = max_tokens or bedrock.get_settings().bedrock_max_tokens
    inference = await bedrock._inference_config(settings_max, temperature, top_p)
    cfg, slug, messages, system, headers = await _prepare(
        model_id, messages, system, purpose=ctx.purpose if ctx else None
    )
    body = _payload(cfg, messages, system, inference)
    url = f"{cfg['base_url']}/chat/completions"
    timeout = int(cfg.get("timeout_s", 60))
    started = time.monotonic()

    async def call() -> dict:
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    return resp.json()
            except Exception as exc:  # noqa: BLE001 — classified below
                last_exc = exc
                if _retryable(exc) and attempt < MAX_ATTEMPTS - 1:
                    logger.warning(
                        "external endpoint %s retry %d: %s", slug, attempt + 1, exc
                    )
                    await asyncio.sleep(min(2**attempt, 8))
                    continue
                raise
        raise last_exc  # pragma: no cover

    prompt = bedrock._prompt_text(messages, system)
    try:
        data = await call()
    except Exception as exc:
        error = ExternalEndpointError(cfg.get("label", slug), slug, str(exc)[:200])
        if ctx is not None:
            bedrock._emit(
                bedrock._record_invocation(
                    ctx, model_id=model_id, prompt=prompt, response_text="",
                    input_tokens=0, output_tokens=0, stop_reason=None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    success=False, error_class=type(exc).__name__,
                )
            )
        raise error from exc

    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    stop_reason = _STOP_REASONS.get(finish, finish)
    usage = data.get("usage") or {}
    estimated = not usage
    input_tokens = usage.get("prompt_tokens") or _estimate_tokens(prompt)
    output_tokens = usage.get("completion_tokens") or _estimate_tokens(text)
    if ctx is not None:
        bedrock._emit(
            bedrock._record_invocation(
                ctx, model_id=model_id, prompt=prompt, response_text=text,
                input_tokens=input_tokens, output_tokens=output_tokens,
                stop_reason=stop_reason,
                latency_ms=int((time.monotonic() - started) * 1000),
                success=True, usage_estimated=estimated,
            )
        )
    # Match bedrock.converse's usage dict shape for callers that read it
    return text, {"inputTokens": input_tokens, "outputTokens": output_tokens}, stop_reason


async def stream_converse_ext(
    *,
    messages: list[dict],
    system: str,
    model_id: str,
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None,
    result,
    ctx,
    enforce: bool,
) -> AsyncGenerator[str, None]:
    """Streaming external call mirroring bedrock.stream_converse's contract.

    Retries only before the first token (identical to the Bedrock policy);
    after first token a failure terminates the stream with the structured
    error — the caller's SSE layer forwards it as an error event.
    """
    from app.services import bedrock

    if ctx is not None and enforce:
        await bedrock.preflight_model_call(ctx, streaming=True)
    settings_max = max_tokens or bedrock.get_settings().bedrock_max_tokens
    inference = await bedrock._inference_config(settings_max, temperature, top_p)
    cfg, slug, messages, system, headers = await _prepare(
        model_id, messages, system, purpose=ctx.purpose if ctx else None
    )
    body = {
        **_payload(cfg, messages, system, inference),
        "stream": True,
        # Ask for usage in the final chunk (OpenAI stream_options); endpoints
        # that reject unknown fields are handled by the retry-less fallback
        # below — vLLM/Ollama tolerate it.
        "stream_options": {"include_usage": True},
    }
    url = f"{cfg['base_url']}/chat/completions"
    timeout = int(cfg.get("timeout_s", 60))
    result = result if result is not None else bedrock.StreamResult()
    prompt = bedrock._prompt_text(messages, system)
    started = time.monotonic()
    usage_seen = False

    async def record(success: bool, error_class: str | None = None) -> None:
        if ctx is None:
            return
        input_tokens = result.input_tokens or _estimate_tokens(prompt)
        output_tokens = result.output_tokens or _estimate_tokens(result.text)
        bedrock._emit(
            bedrock._record_invocation(
                ctx, model_id=model_id, prompt=prompt, response_text=result.text,
                input_tokens=input_tokens, output_tokens=output_tokens,
                stop_reason=result.stop_reason,
                latency_ms=result.latency_ms or int((time.monotonic() - started) * 1000),
                success=success, error_class=error_class,
                usage_estimated=not usage_seen,
            )
        )

    first_token = False
    attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread())[:300].decode(errors="replace")
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}: {detail}",
                            request=resp.request, response=resp,
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk_raw = line[5:].strip()
                        if chunk_raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_raw)
                        except ValueError:
                            continue
                        usage = chunk.get("usage")
                        if usage:
                            usage_seen = True
                            result.input_tokens = usage.get("prompt_tokens", 0)
                            result.output_tokens = usage.get("completion_tokens", 0)
                        for choice in chunk.get("choices") or []:
                            finish = choice.get("finish_reason")
                            if finish:
                                result.stop_reason = _STOP_REASONS.get(finish, finish)
                            delta = (choice.get("delta") or {}).get("content")
                            if delta:
                                first_token = True
                                result.text_parts.append(delta)
                                yield delta
            result.latency_ms = int((time.monotonic() - started) * 1000)
            await record(success=True)
            return
        except GeneratorExit:  # client disconnected — record what we have
            await record(success=True)
            raise
        except Exception as exc:  # noqa: BLE001 — classified below
            if _retryable(exc) and not first_token and attempt < MAX_ATTEMPTS - 1:
                attempt += 1
                logger.warning("external stream %s retry %d: %s", slug, attempt, exc)
                await asyncio.sleep(min(2**attempt, 8))
                continue
            await record(success=False, error_class=type(exc).__name__)
            raise ExternalEndpointError(
                cfg.get("label", slug), slug, str(exc)[:200]
            ) from exc
