"""The regenerate route family: retry the last answer, non-streaming and
streaming. See app/routers/messages/_shared.py for the dedup/streaming
engine this module calls into, and app/routers/messages/__init__.py's
module docstring for why `run_orchestrator`/`add_message` are read via a
qualified `_messages.<name>` reference rather than a bare imported name.
"""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Request

import app.routers.messages as _messages
from ...ask_support import _pinned_ask_request
from ...auth import current_owner
from ...context_builder import build_context_prompt
from ...database import delete_messages_after, get_conversation, list_messages
from ...ratelimit import limiter, rate_limit_value
from ...schemas import AskRequest, AskResponse, RegenerateRequest
from ..deps import (
    _encode_academic_results,
    _encode_action,
    _encode_code_results,
    _encode_fact_checks,
    _encode_images,
    _encode_math_results,
    _encode_sources,
    _owned_or_404,
    router,
)
from ._shared import _dedup_or_call, _stream_and_persist


def _prepare_regeneration(
    conversation_id: int, req: RegenerateRequest
) -> tuple[AskRequest, str, int, str]:
    """Build the retry request for the last user turn (without deleting anything).

    Returns (request, context_note, last_user_message_id, routing_question). The
    old answer is deleted only once the new one is ready, so a failed retry loses
    nothing. `routing_question` is the raw last user turn, used to route on the
    question rather than the assembled history. Raises 400 if the conversation
    has no user message to regenerate.
    """
    conversation = get_conversation(conversation_id)
    messages = list_messages(conversation_id)
    last_user = next(
        (m for m in reversed(messages) if m["role"] == "user"),
        None,
    )
    if last_user is None:
        raise HTTPException(
            status_code=400, detail="No user message to regenerate an answer for."
        )

    last_user_id = int(last_user["id"])
    last_user_question = str(last_user["content"])
    prior = [m for m in messages if int(m["id"]) < last_user_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=last_user_question,
        system_prompt=conversation.get("system_prompt") if conversation else None,
    )

    # Reuse whatever images/files the original turn was asked with, so a retry
    # sees the same vision/document input rather than silently losing it.
    raw_images = last_user.get("images")
    last_user_images = json.loads(str(raw_images)) if raw_images else None
    raw_files = last_user.get("files")
    last_user_files = json.loads(str(raw_files)) if raw_files else None

    raw_req = AskRequest(
        question=context_question,
        mode=req.mode,
        no_cache=True,  # a regeneration is always fresh (no cache read or write)
        model=req.model,
        images=last_user_images,
        files=last_user_files,
    )
    # Apply the conversation's model pin, same as every other ask-path (ask,
    # ask/stream, edit) — this was the one path that forgot to, so a pinned
    # conversation's regenerate silently ignored the pin and routed by
    # req.mode/req.model instead.
    contextual_req = (
        _pinned_ask_request(conversation, context_question, raw_req)
        if conversation
        else raw_req
    )
    context_note = f"regenerated | context_messages={len(prior)}"
    return contextual_req, context_note, last_user_id, last_user_question


@router.post(
    "/v1/conversations/{conversation_id}/regenerate", response_model=AskResponse
)
@limiter.limit(rate_limit_value)
def regenerate_conversation(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)

    def compute() -> AskResponse:
        return _regenerate_conversation_impl(conversation_id, req, owner)

    return _dedup_or_call(req.request_id, compute)


def _regenerate_conversation_impl(
    conversation_id: int, req: RegenerateRequest, owner: str | None
) -> AskResponse:
    """See _dedup_or_call's docstring — split out for the same reason as
    _ask_conversation_impl."""
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )

    result = _messages.run_orchestrator(
        contextual_req, routing_question=routing_question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | {context_note}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
        code_results=result.code_results,
        fact_checks=result.fact_checks,
        academic_results=result.academic_results,
        model=result.model,
        math_results=result.math_results,
        truncated=result.truncated,
    )

    if response.answer.strip():
        # Success: swap in the new answer. On failure, keep the existing answer.
        delete_messages_after(conversation_id, last_user_id)
        _messages.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response.answer,
            mode_used=response.mode_used,
            notes=response.notes,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            cached=response.cached,
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
            fact_checks=_encode_fact_checks(response.fact_checks),
            academic_results=_encode_academic_results(response.academic_results),
            model=response.model,
            math_results=_encode_math_results(response.math_results),
        )

    return response


@router.post("/v1/conversations/{conversation_id}/regenerate/stream")
@limiter.limit(rate_limit_value)
def regenerate_conversation_stream(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        replace_after_id=last_user_id,
        routing_question=routing_question,
        owner=owner,
        request_id=req.request_id,
    )
