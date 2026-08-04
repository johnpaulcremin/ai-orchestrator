"""The ask/continue route family: continue a truncated answer, ask a new
question (non-streaming and streaming). See app/routers/messages/_shared.py
for the dedup/streaming engine this module calls into, and
app/routers/messages/__init__.py's module docstring for why
`run_orchestrator`/`run_workflow`/`add_message` are read via a qualified
`_messages.<name>` reference rather than a bare imported name.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request

import app.routers.messages as _messages
from ...audio_ingestion import resolve_audio_attachments
from ...spreadsheet_ingestion import resolve_xlsx_attachments
from ...ask_support import (
    _is_context_free,
    _is_generic_title,
    _library_stage_timing,
    _memory_stage_timing,
    _pinned_ask_request,
    _recall_library,
    _recall_memory,
    _title_from_question,
)
from ... import memory
from ...auth import current_owner
from ...correction_tracking import record_if_correction
from ...context_builder import (
    build_context_prompt,
    build_context_prompt_with_cache_split,
    build_recent_history_snippet,
)
from ...database import append_to_message, list_messages, update_conversation_title
from ...ratelimit import limiter, rate_limit_value
from ...schemas import AskRequest, AskResponse, MessageOut, Mode
from ..deps import (
    _encode_academic_results,
    _encode_action,
    _encode_audio,
    _encode_code_results,
    _encode_fact_checks,
    _encode_files,
    _encode_images,
    _encode_library_sources,
    _encode_math_results,
    _encode_sources,
    _encode_workflow_steps,
    _owned_or_404,
    router,
)
from ._shared import _dedup_or_call, _stream_and_persist, _stream_workflow_and_persist


def _continuation_prompt(prior_content: str) -> str:
    return (
        "Continue your previous answer in this conversation EXACTLY where it "
        "left off. Do not repeat any part of it, and do not add a preamble, "
        "acknowledgement, or restated heading — your reply must pick up "
        "mid-sentence (or mid-code) exactly as if the text below never "
        "stopped.\n\n"
        "Your answer so far, cut off mid-way:\n"
        f"{prior_content}"
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/continue",
    response_model=MessageOut,
)
@limiter.limit(rate_limit_value)
def continue_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    request_id: str | None = None,
    owner: str | None = Depends(current_owner),
):
    """Resume a message that got cut off at max_output_tokens.

    Non-streaming by design (unlike ask/regenerate/edit): a continuation is a
    short, occasional follow-up action, not the primary answering path, so a
    second streaming implementation isn't worth the added surface here. The
    continuation is appended to the SAME message row rather than creating a
    new one — from the user's point of view they asked one question and got
    one (possibly multi-part) answer.

    `request_id` (see app/request_registry.py) is a query param rather than
    a body field, unlike every other idempotent endpoint here — this route
    has never taken a request body (nothing about a continuation is
    client-supplied beyond which message), and adding one just to carry a
    single optional string isn't worth the API-shape change.
    """
    conversation = _owned_or_404(conversation_id, owner)

    def compute() -> dict[str, Any]:
        return _continue_message_impl(conversation, conversation_id, message_id, owner)

    return _dedup_or_call(request_id, compute)


def _continue_message_impl(
    conversation: dict, conversation_id: int, message_id: int, owner: str | None
) -> dict[str, Any]:
    """See _dedup_or_call's docstring — split out for the same reason as
    _ask_conversation_impl. Returns the raw dict append_to_message gives
    back (not a MessageOut instance) — response_model=MessageOut on
    continue_message already handles serialization for the original
    caller; a duplicate caller gets the identical dict back from
    request_registry, same as every other _dedup_or_call-wrapped endpoint.
    """
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "assistant":
        raise HTTPException(
            status_code=400, detail="Only an assistant message can be continued"
        )
    if not target.get("truncated"):
        raise HTTPException(status_code=400, detail="Message was not truncated")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=_continuation_prompt(str(target["content"])),
        system_prompt=conversation.get("system_prompt"),
    )
    base_req = AskRequest(question=context_question, mode=Mode.auto, no_cache=True)
    contextual_req = _pinned_ask_request(conversation, context_question, base_req)

    result = _messages.run_orchestrator(contextual_req, owner=owner)

    if not result.answer.strip():
        raise HTTPException(
            status_code=502, detail=result.notes or "Continuation failed"
        )

    updated = append_to_message(
        conversation_id=conversation_id,
        message_id=message_id,
        additional_content=result.answer,
        truncated=result.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


@router.post("/v1/conversations/{conversation_id}/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask_conversation(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

    def compute() -> AskResponse:
        return _ask_conversation_impl(conversation_id, req, owner, conversation)

    return _dedup_or_call(req.request_id, compute)


def _ask_conversation_impl(
    conversation_id: int,
    req: AskRequest,
    owner: str | None,
    conversation: dict,
) -> AskResponse:
    """The actual ask work — see _dedup_or_call's docstring for why this is
    split out of ask_conversation: a duplicate request_id must call this
    ZERO times, so it can't live inline in the route function's body."""
    prior_messages = list_messages(conversation_id)
    record_if_correction(owner, prior_messages, req.question)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    # Convert any attached .xlsx into a plain-text table, then transcribe
    # any attached audio, BEFORE anything else touches req.files —
    # everything downstream (persistence, context building, the model call
    # itself) reads req.files, so both conversions have to happen here, the
    # one place that knows the request's original attachments. See
    # app/spreadsheet_ingestion.py and app/audio_ingestion.py.
    req.files = resolve_xlsx_attachments(req.files)
    req.files, audio_meta = resolve_audio_attachments(req.audio, req.files, owner)

    _messages.add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
        images=_encode_images(req.images),
        files=_encode_files(req.files),
        audio=_encode_audio(audio_meta),
    )

    if req.mode == Mode.workflow:
        # Opt-in workflow mode operates on the raw new turn only — no
        # conversation history/memory/library context threading (see
        # app/workflow.py's module docstring) — so it skips straight past
        # the ordinary context-assembly pipeline below.
        result = _messages.run_workflow(req, owner=owner)
        response = AskResponse(
            answer=result.answer,
            mode_used=result.mode_used,
            notes=f"{result.notes} | context_messages={len(prior_messages)}",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            cached=result.cached,
            truncated=result.truncated,
            workflow_steps=result.workflow_steps,
        )
        if response.answer.strip():
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
                truncated=response.truncated,
                workflow_steps=_encode_workflow_steps(response.workflow_steps),
            )
        return response

    memory_vector, memory_snippets, memory_ms = _recall_memory(
        req.question, owner, conversation_id
    )
    library_snippets, library_sources, library_ms = _recall_library(req.question, owner)

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
            library_snippets=library_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    # Route on the new user turn, not the assembled context prompt.
    result = _messages.run_orchestrator(
        contextual_req,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
        pre_stage_timings={
            **(_memory_stage_timing(memory_ms) or {}),
            **(_library_stage_timing(library_ms) or {}),
        }
        or None,
        library_sources=library_sources or None,
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | context_messages={len(prior_messages)}",
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
        library_sources=result.library_sources,
        truncated=result.truncated,
    )

    # Only persist a real answer: an empty/failed reply (auth error, rate limit,
    # all fallbacks exhausted) must not write an empty assistant bubble. The user
    # turn is already saved and the failure is returned to the client in `notes`.
    if response.answer.strip():
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
            library_sources=_encode_library_sources(response.library_sources),
        )
        memory.remember(
            owner, conversation_id, req.question, response.answer, memory_vector
        )

    return response


@router.post("/v1/conversations/{conversation_id}/ask/stream")
@limiter.limit(rate_limit_value)
def ask_conversation_stream(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

    prior_messages = list_messages(conversation_id)
    record_if_correction(owner, prior_messages, req.question)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    # See _ask_conversation_impl's identical step for why this runs before
    # req.files is read by anything else.
    req.files = resolve_xlsx_attachments(req.files)
    req.files, audio_meta = resolve_audio_attachments(req.audio, req.files, owner)

    _messages.add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
        images=_encode_images(req.images),
        files=_encode_files(req.files),
        audio=_encode_audio(audio_meta),
    )

    if req.mode == Mode.workflow:
        context_note = f"context_messages={len(prior_messages)}"
        return _stream_workflow_and_persist(
            conversation_id, req, context_note, owner=owner
        )

    memory_vector, memory_snippets, memory_ms = _recall_memory(
        req.question, owner, conversation_id
    )
    library_snippets, library_sources, library_ms = _recall_library(req.question, owner)

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
            library_snippets=library_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    context_note = f"context_messages={len(prior_messages)}"

    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
        remember_memory=True,
        memory_question=req.question,
        memory_vector=memory_vector,
        request_id=req.request_id,
        pre_stage_timings={
            **(_memory_stage_timing(memory_ms) or {}),
            **(_library_stage_timing(library_ms) or {}),
        }
        or None,
        library_sources=library_sources or None,
    )
