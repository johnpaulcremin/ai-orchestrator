from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from openai import BadRequestError

from . import cache
from .observability import enrich_span
from .providers import (
    AUTH_ERRORS,
    RATE_ERRORS,
    call_anthropic,
    call_litellm,
    key_env_for,
    provider_of,
    stream_anthropic,
    stream_litellm,
)
from .routing import decide_route
from .schemas import AskRequest, AskResponse
from .settings import model_setting
from .telemetry import elapsed_ms, logger, new_request_meta
from .usage import Usage, estimate_cost

load_dotenv()

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Create the OpenAI client on first use so the module imports without a key."""
    global _client

    if _client is None:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Check your .env and shell env vars."
            )
        _client = OpenAI(api_key=api_key)

    return _client


def _timeout_seconds() -> float:
    """Request timeout for answer calls. Tolerates missing or malformed values."""
    raw = (os.getenv("OPENAI_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    return value if value > 0 else 120.0


def _fallback_models(primary_model: str) -> list[str]:
    """
    Ordered fallback candidates.

    OPENAI_MODEL_FALLBACK is optional.
    If it is not set, we fall back to OPENAI_MODEL_FAST, then OPENAI_MODEL.
    Duplicates and the primary model are removed.
    """
    # Resolve through the settings layer so a saved override for any of these
    # keys is honoured in the fallback chain, not just the env var. Mirror
    # routing's defaults: FAST falls back to the base model, and the base keeps
    # its "gpt-5" code default so it's always a final fallback candidate (without
    # it, overriding only a tier while leaving OPENAI_MODEL unset would leave the
    # chain empty).
    base = model_setting("OPENAI_MODEL", "gpt-5")
    candidates = [
        model_setting("OPENAI_MODEL_FALLBACK"),
        model_setting("OPENAI_MODEL_FAST", base),
        base,
    ]

    seen: set[str] = set()
    fallbacks: list[str] = []

    for model in candidates:
        if not model:
            continue
        if model == primary_model:
            continue
        if model in seen:
            continue

        seen.add(model)
        fallbacks.append(model)

    return fallbacks


def _extract_text(result: object) -> str:
    answer_text = getattr(result, "output_text", None) or ""
    if not answer_text:
        answer_text = str(result)
    return answer_text.strip()


_SUMMARY_PROMPT = (
    "Summarize the earlier part of a conversation into compact notes the "
    "assistant can rely on later. Preserve facts, decisions, names, numbers, and "
    "anything the user might refer back to. Be concise and omit pleasantries.\n\n"
    "Conversation excerpt:\n{text}"
)

# Cap on the transcript fed to the summarizer, to bound cost. When the older
# window is larger than this, keep the TAIL (the most recent of the older turns,
# which are the most relevant) rather than truncating to the oldest.
_SUMMARY_INPUT_CHARS = 24000


def _summary_max_tokens() -> int:
    raw = (os.getenv("SUMMARY_MAX_OUTPUT_TOKENS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 600
    return value if value > 0 else 600


def summarize_text(text: str) -> str:
    """Summarize text with the cheap router model. Returns '' on any failure.

    Used to fold older conversation turns into a memory summary. It never raises,
    so a missing key / model error simply omits the summary.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    try:
        client = get_client()
    except RuntimeError:
        return ""

    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano")
    # Keep the most recent slice of the older window (see _SUMMARY_INPUT_CHARS).
    prompt = _SUMMARY_PROMPT.format(text=clean[-_SUMMARY_INPUT_CHARS:])
    # Best-effort + on the pre-answer critical path: fail fast (no SDK retries)
    # and a modest timeout, so a slow endpoint can't stall the answer for long.
    timeout_client = client.with_options(timeout=12.0, max_retries=0)

    def _create(**extra: object) -> object:
        return timeout_client.responses.create(
            model=router_model,
            input=prompt,
            max_output_tokens=_summary_max_tokens(),
            **extra,
        )

    try:
        # Minimal reasoning keeps the summary call cheap, like the router.
        result = _create(reasoning={"effort": "minimal"})
    except BadRequestError:
        try:
            result = _create()
        except Exception:
            return ""
    except Exception:
        return ""

    return (getattr(result, "output_text", None) or "").strip()


class _ModelStreamError(Exception):
    """Raised when the streaming API reports a terminal failure event."""


def _usage_fields(model: str, usage: Usage) -> dict:
    """AskResponse/done-event usage fields, or empty if no tokens were captured."""
    if usage.total_tokens == 0:
        return {}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": estimate_cost(model, usage),
    }


def _record_openai_usage(result: object, usage: Usage | None) -> None:
    if usage is None:
        return
    source = getattr(result, "usage", None)
    if source is not None:
        usage.input_tokens = int(getattr(source, "input_tokens", 0) or 0)
        usage.output_tokens = int(getattr(source, "output_tokens", 0) or 0)


def _call_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
) -> str:
    client = get_client().with_options(timeout=_timeout_seconds())

    if reasoning_effort:
        try:
            result = client.responses.create(
                model=model,
                input=question,
                max_output_tokens=max_output_tokens,
                reasoning={"effort": reasoning_effort},
            )
            _record_openai_usage(result, usage)
            return _extract_text(result)
        except BadRequestError:
            # Some models reject the reasoning param; retry once without it.
            logger.warning(
                "request.reasoning_rejected model=%s effort=%s retrying_without_reasoning",
                model,
                reasoning_effort,
            )

    result = client.responses.create(
        model=model,
        input=question,
        max_output_tokens=max_output_tokens,
    )
    _record_openai_usage(result, usage)
    return _extract_text(result)


def _stream_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
) -> Iterator[str]:
    """Yield output text deltas from a streaming Responses API call."""
    client = get_client().with_options(timeout=_timeout_seconds())

    stream = None
    if reasoning_effort:
        try:
            stream = client.responses.create(
                model=model,
                input=question,
                max_output_tokens=max_output_tokens,
                reasoning={"effort": reasoning_effort},
                stream=True,
            )
        except BadRequestError:
            # Some models reject the reasoning param; retry once without it.
            logger.warning(
                "stream.reasoning_rejected model=%s effort=%s retrying_without_reasoning",
                model,
                reasoning_effort,
            )

    if stream is None:
        stream = client.responses.create(
            model=model,
            input=question,
            max_output_tokens=max_output_tokens,
            stream=True,
        )

    for event in stream:
        event_type = getattr(event, "type", "")

        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                yield delta
        elif event_type == "response.completed":
            _record_openai_usage(getattr(event, "response", None), usage)
        elif event_type == "response.failed":
            response = getattr(event, "response", None)
            error = getattr(response, "error", None)
            message = getattr(error, "message", "") or "Model response failed."
            raise _ModelStreamError(message)
        elif event_type == "error":
            message = getattr(event, "message", "") or "Model stream error."
            raise _ModelStreamError(message)


def _call_model(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
) -> str:
    """Dispatch a non-streaming call to the provider that owns the model."""
    provider = provider_of(model)
    if provider == "anthropic":
        return call_anthropic(
            model, question, max_output_tokens, _timeout_seconds(), usage
        )
    if provider == "litellm":
        return call_litellm(
            model,
            question,
            max_output_tokens,
            _timeout_seconds(),
            reasoning_effort,
            usage,
        )
    return _call_openai(model, question, max_output_tokens, reasoning_effort, usage)


def _stream_model(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
) -> Iterator[str]:
    """Dispatch a streaming call to the provider that owns the model."""
    provider = provider_of(model)
    if provider == "anthropic":
        yield from stream_anthropic(
            model, question, max_output_tokens, _timeout_seconds(), usage
        )
        return
    if provider == "litellm":
        yield from stream_litellm(
            model,
            question,
            max_output_tokens,
            _timeout_seconds(),
            reasoning_effort,
            usage,
        )
        return
    yield from _stream_openai(
        model, question, max_output_tokens, reasoning_effort, usage
    )


def _auth_key_env(model: str) -> str:
    """The env var whose key an auth failure for this model implicates."""
    return key_env_for(model)


def _cache_key(req: AskRequest) -> str | None:
    """The cache key for this request, or None when the cache should be skipped.

    Skipped entirely (no read AND no write) when:
    - caching is off;
    - a model is forced (the key doesn't encode it, so caching would read or
      poison the normally-routed entry); or
    - no_cache is set (e.g. regenerate) — a one-off fresh answer must neither be
      served from nor written into the shared, un-owner-scoped cache.
    """
    if not cache.enabled() or req.model or req.no_cache:
        return None
    return cache.make_key(req.question, req.mode.value)


def _cached_hit_note(hit: dict, meta: object, ms: int) -> str:
    original = hit.get("mode_used") or "?"
    saved = hit.get("cost_usd")
    saved_note = (
        f", saved≈${saved:.4f}" if isinstance(saved, (int, float)) and saved else ""
    )
    return (
        f"Served from response cache (originally {original}{saved_note}) "
        f"| request_id={getattr(meta, 'request_id', '?')} | ms={ms}"
    )


def _cached_response(hit: dict, meta: object, ms: int) -> AskResponse:
    return AskResponse(
        answer=str(hit.get("answer") or ""),
        mode_used=str(hit.get("mode_used") or "cache"),
        notes=_cached_hit_note(hit, meta, ms),
        cost_usd=0.0,
        cached=True,
    )


def run_orchestrator(req: AskRequest) -> AskResponse:
    meta = new_request_meta()

    key = _cache_key(req)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            logger.info(
                "request.cache_hit id=%s ms=%s model=%s",
                meta.request_id,
                ms,
                hit.get("model"),
            )
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": str(hit.get("mode_used") or ""),
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                }
            )
            return _cached_response(hit, meta, ms)

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("request.no_api_key id=%s", meta.request_id)
        return AskResponse(
            answer="",
            mode_used=str(req.mode.value),
            notes=f"{e} | request_id={meta.request_id}",
        )

    decision = decide_route(
        req.question, req.mode, client=client, forced_model=req.model
    )

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
        }
    )

    logger.info(
        "request.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    usage = Usage()

    try:
        answer_text = _call_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
        )

        ms = elapsed_ms(meta)

        logger.info(
            "request.ok id=%s ms=%s model=%s tokens=%s",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
        )

        response = AskResponse(
            answer=answer_text,
            mode_used=decision.mode_used,
            notes=f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
            **_usage_fields(decision.model, usage),
        )
        if key is not None:
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_text,
                decision.mode_used,
                response.notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        return response

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("request.auth_failed id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
        )

    except RATE_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("request.rate_limited id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Rate limited / quota exceeded. | request_id={meta.request_id} | ms={ms}",
        )

    except Exception as primary_error:
        logger.exception(
            "request.primary_model_failed id=%s model=%s err=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
        )

        fallbacks = _fallback_models(decision.model)

        for fallback_model in fallbacks:
            try:
                logger.info(
                    "request.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                fallback_usage = Usage()
                answer_text = _call_model(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                )

                ms = elapsed_ms(meta)

                logger.info(
                    "request.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_response = AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=(
                        f"{decision.notes} | primary_model={decision.model} failed with "
                        f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                    **_usage_fields(fallback_model, fallback_usage),
                )
                if key is not None:
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        answer_text,
                        fallback_response.mode_used,
                        fallback_response.notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                return fallback_response

            except Exception as fallback_error:
                logger.exception(
                    "request.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )

        ms = elapsed_ms(meta)

        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=(
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| request_id={meta.request_id} | ms={ms}"
            ),
        )


def stream_orchestrator(req: AskRequest) -> Iterator[dict[str, Any]]:
    """
    Streaming variant of run_orchestrator.

    Yields plain dicts {"event": str, "data": dict} matching the SSE contract:
    one "meta" event, zero or more "delta" events, then a terminal "done" or
    "error" event. Persistence and wire formatting are the caller's job; this
    function never touches the database.
    """
    meta = new_request_meta()

    key = _cache_key(req)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            answer = str(hit.get("answer") or "")
            mode_used = str(hit.get("mode_used") or "cache")
            logger.info("stream.cache_hit id=%s ms=%s", meta.request_id, ms)
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": mode_used,
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                    "ai.streaming": True,
                }
            )
            yield {
                "event": "meta",
                "data": {
                    "request_id": meta.request_id,
                    "mode_used": mode_used,
                    "model": str(hit.get("model") or ""),
                    "notes": "cache=hit",
                },
            }
            if answer:
                yield {"event": "delta", "data": {"text": answer}}
            yield {
                "event": "done",
                "data": {
                    "answer": answer,
                    "mode_used": mode_used,
                    "notes": _cached_hit_note(hit, meta, ms),
                    "cached": True,
                },
            }
            return

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("stream.no_api_key id=%s", meta.request_id)
        yield {"event": "error", "data": {"message": str(e)}}
        return

    decision = decide_route(
        req.question, req.mode, client=client, forced_model=req.model
    )

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
            "ai.streaming": True,
        }
    )

    logger.info(
        "stream.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    yield {
        "event": "meta",
        "data": {
            "request_id": meta.request_id,
            "mode_used": decision.mode_used,
            "model": decision.model,
            "notes": decision.notes,
        },
    }

    streamed_any = False
    accumulated: list[str] = []
    usage = Usage()

    try:
        for text in _stream_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
        ):
            streamed_any = True
            accumulated.append(text)
            yield {"event": "delta", "data": {"text": text}}

        ms = elapsed_ms(meta)

        logger.info(
            "stream.ok id=%s ms=%s model=%s tokens=%s",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
        )

        answer_final = "".join(accumulated).strip()
        done_notes = f"{decision.notes} | request_id={meta.request_id} | ms={ms}"
        if key is not None:
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_final,
                decision.mode_used,
                done_notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )

        yield {
            "event": "done",
            "data": {
                "answer": answer_final,
                "mode_used": decision.mode_used,
                "notes": done_notes,
                **_usage_fields(decision.model, usage),
            },
        }
        return

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("stream.auth_failed id=%s ms=%s", meta.request_id, ms)
        yield {
            "event": "error",
            "data": {
                "message": f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    except RATE_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("stream.rate_limited id=%s ms=%s", meta.request_id, ms)
        yield {
            "event": "error",
            "data": {
                "message": f"Rate limited / quota exceeded. | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    except Exception as primary_error:
        if streamed_any:
            # Partial output already went out; no fallback is possible.
            ms = elapsed_ms(meta)
            logger.exception(
                "stream.interrupted id=%s ms=%s model=%s err=%s",
                meta.request_id,
                ms,
                decision.model,
                type(primary_error).__name__,
            )
            yield {
                "event": "error",
                "data": {
                    "message": (
                        f"Stream interrupted: {type(primary_error).__name__}: {primary_error} "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                },
            }
            return

        logger.exception(
            "stream.primary_model_failed id=%s model=%s err=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
        )

        for fallback_model in _fallback_models(decision.model):
            fallback_parts: list[str] = []
            fallback_usage = Usage()

            try:
                logger.info(
                    "stream.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                for text in _stream_model(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                ):
                    fallback_parts.append(text)
                    yield {"event": "delta", "data": {"text": text}}

                ms = elapsed_ms(meta)

                logger.info(
                    "stream.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_answer = "".join(fallback_parts).strip()
                fallback_notes = (
                    f"{decision.notes} | primary_model={decision.model} failed with "
                    f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                    f"| request_id={meta.request_id} | ms={ms}"
                )
                if key is not None:
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        fallback_answer,
                        f"{decision.mode_used}->fallback",
                        fallback_notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )

                yield {
                    "event": "done",
                    "data": {
                        "answer": fallback_answer,
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": fallback_notes,
                        **_usage_fields(fallback_model, fallback_usage),
                    },
                }
                return

            except Exception as fallback_error:
                logger.exception(
                    "stream.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )

                if fallback_parts:
                    # This fallback streamed partial output; stop entirely.
                    ms = elapsed_ms(meta)
                    yield {
                        "event": "error",
                        "data": {
                            "message": (
                                f"Stream interrupted: {type(fallback_error).__name__}: {fallback_error} "
                                f"| request_id={meta.request_id} | ms={ms}"
                            ),
                        },
                    }
                    return

        ms = elapsed_ms(meta)

        yield {
            "event": "error",
            "data": {
                "message": (
                    f"Primary model failed and no fallback succeeded. "
                    f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                    f"| request_id={meta.request_id} | ms={ms}"
                ),
            },
        }
        return
