from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI
from openai import APIError, AuthenticationError, BadRequestError, RateLimitError

from .routing import decide_route
from .schemas import AskRequest, AskResponse
from .telemetry import elapsed_ms, logger, new_request_meta

load_dotenv()

api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not set. Check your .env and shell env vars.")

client = OpenAI(api_key=api_key)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


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


def _call_openai(model: str, question: str, max_output_tokens: int) -> str:
    result = client.responses.create(
        model=model,
        input=question,
        max_output_tokens=max_output_tokens,
    )

    return _extract_text(result)


def run_orchestrator(req: AskRequest) -> AskResponse:
    meta = new_request_meta()
    decision = decide_route(req.question, req.mode, client=client)

    logger.info(
        "request.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    try:
        answer_text = _call_openai(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
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

    except AuthenticationError:
        ms = elapsed_ms(meta)
        logger.exception("request.auth_failed id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"OpenAI authentication failed. Check OPENAI_API_KEY. | request_id={meta.request_id} | ms={ms}",
        )

    except RateLimitError:
        ms = elapsed_ms(meta)
        logger.exception("request.rate_limited id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Rate limited / quota exceeded. | request_id={meta.request_id} | ms={ms}",
        )

    except (BadRequestError, APIError) as primary_error:
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

                answer_text = _call_openai(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
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

    except Exception as e:
        ms = elapsed_ms(meta)
        logger.exception(
            "request.unexpected_error id=%s ms=%s err=%s",
            meta.request_id,
            ms,
            type(e).__name__,
        )
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Unexpected server error: {type(e).__name__}: {e} | request_id={meta.request_id} | ms={ms}",
        )
