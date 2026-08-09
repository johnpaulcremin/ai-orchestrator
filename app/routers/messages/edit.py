"""The edit route family: edit a past user message and re-answer,
non-streaming and streaming. See app/routers/messages/_shared.py for the
dedup/streaming engine this module calls into, and app/routers/messages/
__init__.py's module docstring for why `run_orchestrator`/`add_message` are
read via a qualified `_messages.<name>` reference rather than a bare
imported name.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

import app.routers.messages as _messages
from ... import retry_attribution
from ...ask_support import _pinned_ask_request
from ...auth import current_owner
from ...context_builder import build_context_prompt
from ...database import delete_messages_from, list_messages
from ...ratelimit import limiter, rate_limit_value
from ...schemas import AskRequest, AskResponse
from ..deps import (
    _encode_academic_results,
    _encode_action,
    _encode_code_results,
    _encode_fact_checks,
    _encode_files,
    _encode_images,
    _encode_math_results,
    _encode_sources,
    _owned_or_404,
    router,
)
from ._shared import _dedup_or_call, _stream_and_persist


def _prepare_edit(
    conversation: dict, conversation_id: int, message_id: int, req: AskRequest
) -> tuple[AskRequest, str, str]:
    """Build the retry request for editing message_id (without deleting
    anything yet).

    Returns (request, context_note, routing_question). Context is built from
    only the messages BEFORE message_id — the edited message and everything
    after it are deleted only once the new answer is ready (see
    _stream_and_persist's edit_message_id), so a failed edit loses nothing.
    Raises 404 if the message doesn't belong to this conversation, 400 if
    it isn't a user message (only a user turn can be edited).
    """
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "user":
        raise HTTPException(status_code=400, detail="Only a user message can be edited")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=req.question,
        system_prompt=conversation.get("system_prompt"),
    )
    contextual_req = _pinned_ask_request(conversation, context_question, req)
    context_note = f"edited | context_messages={len(prior)}"
    return contextual_req, context_note, req.question


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/edit",
    response_model=AskResponse,
)
@limiter.limit(rate_limit_value)
def edit_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

    def compute() -> AskResponse:
        return _edit_message_impl(conversation, conversation_id, message_id, req, owner)

    return _dedup_or_call(req.request_id, compute)


def _edit_message_impl(
    conversation: dict,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None,
) -> AskResponse:
    """See _dedup_or_call's docstring — split out for the same reason as
    _ask_conversation_impl."""
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
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
        # Success: swap in the edited message and its new answer. On failure,
        # keep the original message and answer untouched.
        #
        # Snapshotted before the delete for the same reason as regenerate's
        # (see app/retry_attribution.py) — with one difference that matters:
        # this path deletes the USER row too and re-inserts it under a new id,
        # so the new id is handed to record_retry to keep the turn's attempt
        # chain joined across the edit. Measurement only.
        snapshot = retry_attribution.snapshot_turn(conversation_id, message_id)
        delete_messages_from(conversation_id, message_id)
        new_user_message = _messages.add_message(
            conversation_id=conversation_id,
            role="user",
            content=req.question,
            images=_encode_images(req.images),
            files=_encode_files(req.files),
        )
        new_message = _messages.add_message(
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
        retry_attribution.record_retry(
            owner,
            conversation_id,
            snapshot,
            kind="edit",
            new_message_id=int(new_message["id"]) if new_message else None,
            new_user_message_id=(
                int(new_user_message["id"]) if new_user_message else None
            ),
            mode_used=response.mode_used,
            model=response.model,
            cost_usd=response.cost_usd,
        )

    return response


@router.post("/v1/conversations/{conversation_id}/messages/{message_id}/edit/stream")
@limiter.limit(rate_limit_value)
def edit_message_stream(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=routing_question,
        owner=owner,
        request_id=req.request_id,
        edit_message_id=message_id,
        edit_question=req.question,
        edit_images=req.images,
        edit_files=req.files,
    )
