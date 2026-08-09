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
from ... import retry_attribution
from ...ask_support import _pinned_ask_request
from ...auth import current_owner
from ...context_builder import build_context_prompt
from ...database import delete_messages_after, get_conversation, list_messages
from ...ratelimit import limiter, rate_limit_value
from ...schemas import AskRequest, AskResponse, Mode, RegenerateRequest
from ..deps import _owned_or_404, router
from ._shared import (
    _api_response,
    _dedup_or_call,
    _persist_assistant_message,
    _stream_and_persist,
    _stream_workflow_and_persist,
)


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

    # mode="workflow" is honoured here, exactly as the ask path honours it.
    # It used to be accepted by RegenerateRequest and then silently ignored:
    # this function called run_orchestrator unconditionally, and decide_route
    # has no Mode.workflow case, so the request fell through to the FAST tier
    # default — a caller who asked for a multi-step answer got a single-shot
    # one at the tightest cap in the app, with nothing in the response saying
    # so.
    result = (
        _messages.run_workflow(contextual_req, owner=owner)
        if contextual_req.mode == Mode.workflow
        else _messages.run_orchestrator(
            contextual_req, routing_question=routing_question, owner=owner
        )
    )

    response = _api_response(result, context_note)

    if response.answer.strip():
        # Success: swap in the new answer. On failure, keep the existing answer.
        #
        # Snapshot the answer being replaced BEFORE the delete: its routing
        # decision and cost exist nowhere else once it's gone, which is the
        # whole reason re-run cost couldn't be measured (see
        # app/retry_attribution.py). Measurement only — nothing below reads it.
        snapshot = retry_attribution.snapshot_turn(conversation_id, last_user_id)
        delete_messages_after(conversation_id, last_user_id)
        new_message = _persist_assistant_message(conversation_id, response)
        retry_attribution.record_retry(
            owner,
            conversation_id,
            snapshot,
            kind="regenerate",
            new_message_id=int(new_message["id"]) if new_message else None,
            mode_used=response.mode_used,
            model=response.model,
            cost_usd=response.cost_usd,
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
    # Same honouring of mode="workflow" as the non-streaming twin above, and
    # for the same reason — a silently-ignored mode is a correctness bug on
    # both halves or neither.
    if contextual_req.mode == Mode.workflow:
        return _stream_workflow_and_persist(
            conversation_id,
            contextual_req,
            context_note,
            owner=owner,
            replace_after_id=last_user_id,
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
