from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from openai import BadRequestError

from .observability import enrich_span
from .providers import (
    AUTH_ERRORS,
    RATE_ERRORS,
    call_anthropic,
    provider_of,
    stream_anthropic,
)
from .routing import decide_route
from .schemas import AskRequest, AskResponse
from .telemetry import elapsed_ms, logger, new_request_meta

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


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


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
    candidates = [
        _env("OPENAI_MODEL_FALLBACK"),
        _env("OPENAI_MODEL_FAST"),
        _env("OPENAI_MODEL"),
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


class _ModelStreamError(Exception):
    """Raised when the streaming API reports a terminal failure event."""


def _call_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
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
    return _extract_text(result)


def _stream_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
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
) -> str:
    """Dispatch a non-streaming call to the provider that owns the model."""
    if provider_of(model) == "anthropic":
        return call_anthropic(model, question, max_output_tokens, _timeout_seconds())
    return _call_openai(model, question, max_output_tokens, reasoning_effort)


def _stream_model(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
) -> Iterator[str]:
    """Dispatch a streaming call to the provider that owns the model."""
    if provider_of(model) == "anthropic":
        yield from stream_anthropic(
            model, question, max_output_tokens, _timeout_seconds()
        )
        return
    yield from _stream_openai(model, question, max_output_tokens, reasoning_effort)


def _auth_key_env(model: str) -> str:
    """The env var whose key an auth failure for this model implicates."""
    return (
        "ANTHROPIC_API_KEY" if provider_of(model) == "anthropic" else "OPENAI_API_KEY"
    )


def run_orchestrator(req: AskRequest) -> AskResponse:
    meta = new_request_meta()

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("request.no_api_key id=%s", meta.request_id)
        return AskResponse(
            answer="",
            mode_used=str(req.mode.value),
            notes=f"{e} | request_id={meta.request_id}",
        )

    decision = decide_route(req.question, req.mode, client=client)

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

    try:
        answer_text = _call_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
        )

        ms = elapsed_ms(meta)

        logger.info(
            "request.ok id=%s ms=%s model=%s",
            meta.request_id,
            ms,
            decision.model,
        )

        return AskResponse(
            answer=answer_text,
            mode_used=decision.mode_used,
            notes=f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
        )

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

                answer_text = _call_model(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                )

                ms = elapsed_ms(meta)

                logger.info(
                    "request.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                return AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=(
                        f"{decision.notes} | primary_model={decision.model} failed with "
                        f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                )

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

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("stream.no_api_key id=%s", meta.request_id)
        yield {"event": "error", "data": {"message": str(e)}}
        return

    decision = decide_route(req.question, req.mode, client=client)

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

    try:
        for text in _stream_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
        ):
            streamed_any = True
            accumulated.append(text)
            yield {"event": "delta", "data": {"text": text}}

        ms = elapsed_ms(meta)

        logger.info(
            "stream.ok id=%s ms=%s model=%s",
            meta.request_id,
            ms,
            decision.model,
        )

        yield {
            "event": "done",
            "data": {
                "answer": "".join(accumulated).strip(),
                "mode_used": decision.mode_used,
                "notes": f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
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

                yield {
                    "event": "done",
                    "data": {
                        "answer": "".join(fallback_parts).strip(),
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": (
                            f"{decision.notes} | primary_model={decision.model} failed with "
                            f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                            f"| request_id={meta.request_id} | ms={ms}"
                        ),
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
