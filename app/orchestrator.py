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


def run_orchestrator(req: AskRequest) -> AskResponse:
    meta = new_request_meta()
    decision = decide_route(req.question, req.mode)

    logger.info(
        "request.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    try:
        result = client.responses.create(
            model=decision.model,
            input=req.question,
            max_output_tokens=decision.max_output_tokens,   
        )

        answer_text = getattr(result, "output_text", None) or ""
        if not answer_text:
            answer_text = str(result)

        ms = elapsed_ms(meta)

        logger.info(
            "request.ok id=%s ms=%s",
            meta.request_id,
            ms,
        )

        return AskResponse(
            answer=answer_text.strip(),
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

    except BadRequestError as e:
        ms = elapsed_ms(meta)
        logger.exception("request.bad_request id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Bad request to OpenAI: {e} | request_id={meta.request_id} | ms={ms}",
        )

    except APIError as e:
        ms = elapsed_ms(meta)
        logger.exception("request.api_error id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"OpenAI API error: {e} | request_id={meta.request_id} | ms={ms}",
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
