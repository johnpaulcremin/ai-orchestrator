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
    _memory_stage_timing,
    _pinned_ask_request,
    _recall_memory,
    _title_from_question,
)
from ... import followup, memory, retry_attribution
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
    _encode_audio,
    _encode_files,
    _encode_images,
    _owned_or_404,
    router,
)
from ._shared import (
    _api_response,
    _dedup_or_call,
    _persist_assistant_message,
    _stream_and_persist,
    _stream_workflow_and_persist,
)


def _followup_routing(
    prior_messages: list[dict[str, Any]], question: str
) -> tuple[str, dict[str, Any]]:
    """(routing question, extra routing kwargs) for this new user turn.

    Ordinary turns route on themselves and may clarify: (question, True). A
    reply to a clarifying question routes on the ORIGINAL request recombined
    with the reply, and may NOT clarify again — see app/followup.py for why
    both halves are needed and why neither alone is enough.

    Only the routing question changes. The answering prompt is untouched: the
    clarifying question and the reply are both already in the conversation
    context build_context_prompt assembles, so the model sees the exchange
    either way. What it could not see was a router that had classified "both"
    as a standalone request.

    The guard is returned as kwargs-to-splat rather than a bare bool so it is
    passed ONLY when it is being cleared, which is exactly how
    allow_auto_workflow is threaded. That asymmetry is deliberate on both
    counts: a caller that does not care about the guard reads identically to
    before, and neither the ordinary call sites nor the many test stubs of
    run_orchestrator have to know the parameter exists.
    """
    combined = followup.clarify_followup(prior_messages, question)
    if combined is None:
        return question, {}
    return f"{followup.ASSUMPTION_INSTRUCTION}\n\n{combined}", {"allow_clarify": False}


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
    # Resume under the routing decision that produced this answer, never
    # Mode.auto — see followup.resume_route for the three separate ways
    # re-classifying a continuation broke the feature. A conversation pin
    # still wins, same as on every other path.
    resume_mode, resume_model = followup.resume_route(
        target.get("mode_used"), target.get("model")
    )
    base_req = AskRequest(
        question=context_question,
        mode=resume_mode,
        model=resume_model,
        no_cache=True,
    )
    contextual_req = _pinned_ask_request(conversation, context_question, base_req)

    # allow_auto_workflow=False: a continuation must resume the answer, never
    # replan it into a fresh multi-step workflow off the back of its own
    # cut-off text (see followup.resume_route).
    result = _messages.run_orchestrator(
        contextual_req, owner=owner, allow_auto_workflow=False
    )

    if not result.answer.strip():
        raise HTTPException(
            status_code=502, detail=result.notes or "Continuation failed"
        )

    # Snapshot BEFORE the append, for the one reason continuations were
    # unmeasurable: append_to_message sums this continuation's cost into the
    # same row, so once it lands the original answer's own cost is gone and the
    # turn's first-attempt cost is unrecoverable. Measurement only — see
    # app/retry_attribution.py.
    snapshot = retry_attribution.snapshot_continuation(conversation_id, message_id)

    updated = append_to_message(
        conversation_id=conversation_id,
        message_id=message_id,
        additional_content=result.answer,
        truncated=result.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        # Replaces, not accumulates: `truncated` above already describes only
        # the continuation's own outcome, so the ceiling stored alongside it
        # has to describe the same attempt or the notice would name a limit
        # some earlier attempt hit instead.
        max_output_tokens=result.max_output_tokens,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")

    # The continuation is an ATTEMPT at this turn: same message row (a
    # continuation has no id of its own), its own cost, and a signal of its own
    # so it is never read as a retry — see retry_attribution.SIGNAL_CONTINUED
    # and retry_cost's split of retries from continuations.
    retry_attribution.record_retry(
        owner,
        conversation_id,
        snapshot,
        kind="continue",
        new_message_id=message_id,
        mode_used=result.mode_used,
        model=result.model,
        cost_usd=result.cost_usd,
    )
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
        #
        # It still honours the conversation's model pin, which this path used to
        # ignore: it passed the raw `req` straight through, so a pinned
        # conversation asked in workflow mode ran every step on the router's own
        # choice of model. Every other answering path applies the pin, and the
        # retry paths apply it to a workflow too — leaving this one out made the
        # pin mean different things depending on which button produced the
        # workflow. `req.question` deliberately, NOT an assembled context prompt:
        # applying the pin must not smuggle history into a mode whose whole
        # premise is the raw turn.
        result = _messages.run_workflow(
            _pinned_ask_request(conversation, req.question, req), owner=owner
        )
        response = _api_response(result, f"context_messages={len(prior_messages)}")
        if response.answer.strip():
            _persist_assistant_message(conversation_id, response)
        return response

    memory_vector, memory_snippets, memory_sources, memory_ms = _recall_memory(
        req.question, owner, conversation_id
    )

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    # Route on the new user turn, not the assembled context prompt — except
    # when that turn is a reply to a clarifying question, which is not a
    # standalone request at all (see _followup_routing).
    routing_question, clarify_guard = _followup_routing(prior_messages, req.question)
    result = _messages.run_orchestrator(
        contextual_req,
        routing_question=routing_question,
        **clarify_guard,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
        pre_stage_timings=_memory_stage_timing(memory_ms),
        # The document library is recalled INSIDE the orchestrator, after
        # routing — it is gated on the classifier's task category, which
        # nothing out here knows yet. See orchestrator._recall_library_context.
        recall_library=True,
        memory_sources=memory_sources or None,
    )

    response = _api_response(result, f"context_messages={len(prior_messages)}")

    # Only persist a real answer: an empty/failed reply (auth error, rate limit,
    # all fallbacks exhausted) must not write an empty assistant bubble. The user
    # turn is already saved and the failure is returned to the client in `notes`.
    if response.answer.strip():
        _persist_assistant_message(conversation_id, response)
        # `memorable` is False when the app appended its own capabilities
        # snapshot (remaining daily budget, free-lane quotas, effective model
        # map) to the answer — see AskResponse.memorable. The conversation
        # still keeps the message; only the durable, cross-conversation copy
        # is skipped, mirroring the response cache's existing refusal.
        if response.memorable:
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
        # Same pin handling and same raw-question rule as the non-streaming
        # twin above — both halves or neither.
        context_note = f"context_messages={len(prior_messages)}"
        return _stream_workflow_and_persist(
            conversation_id,
            _pinned_ask_request(conversation, req.question, req),
            context_note,
            owner=owner,
        )

    memory_vector, memory_snippets, memory_sources, memory_ms = _recall_memory(
        req.question, owner, conversation_id
    )

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    context_note = f"context_messages={len(prior_messages)}"

    routing_question, clarify_guard = _followup_routing(prior_messages, req.question)
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=routing_question,
        allow_clarify=clarify_guard.get("allow_clarify", True),
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
        remember_memory=True,
        memory_question=req.question,
        memory_vector=memory_vector,
        request_id=req.request_id,
        pre_stage_timings=_memory_stage_timing(memory_ms),
        # See _ask_conversation_impl's identical note.
        recall_library=True,
        memory_sources=memory_sources or None,
    )
