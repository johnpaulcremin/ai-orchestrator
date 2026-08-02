"""Message-level and conversation-scoped ask/regenerate/edit/continue
endpoints, plus the context-building helpers they share. See
app/routers/conversations.py for conversation CRUD, and app/routers/ask.py
for the stateless (context-free) /v1/ask, /v1/compare, /v1/estimate.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator, Iterator
from typing import Any, cast

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import memory, request_registry
from ..actions import post_webhook
from ..audio_ingestion import resolve_audio_attachments
from ..ask_support import (
    _is_context_free,
    _is_generic_title,
    _library_stage_timing,
    _memory_stage_timing,
    _pinned_ask_request,
    _recall_library,
    _recall_memory,
    _title_from_question,
)
from ..auth import current_owner
from ..context_builder import (
    build_context_prompt,
    build_context_prompt_with_cache_split,
    build_recent_history_snippet,
)
from ..database import (
    add_message,
    append_to_message,
    claim_pending_action,
    delete_message,
    delete_messages_after,
    delete_messages_from,
    get_conversation,
    get_message,
    list_bookmarked_messages,
    list_messages,
    set_action_status,
    set_message_bookmarked,
    set_message_feedback,
    update_conversation_title,
)
from ..orchestrator import run_orchestrator, stream_orchestrator
from ..ratelimit import limiter, rate_limit_value
from ..telemetry import logger
from ..schemas import (
    ActionConfirmRequest,
    ActionResult,
    AskRequest,
    AskResponse,
    BookmarkedMessage,
    FileAttachment,
    MessageBookmark,
    MessageFeedback,
    MessageOut,
    MessageRestoreRequest,
    Mode,
    RegenerateRequest,
)
from ..workflow import run_workflow, stream_workflow
from .deps import (
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


def _dedup_or_call(request_id: str | None, compute):
    """Idempotency wrapper for the NON-STREAMING ask/regenerate/edit/
    continue endpoints — see app/request_registry.py and
    _stream_and_persist's module docstring for the streaming equivalent and
    the full disconnect-proofing rationale this is one half of.

    The ORIGINAL caller for a given request_id runs `compute()` (which does
    everything: the orchestrator call AND persisting the result) and its
    return value is published for any duplicate. A DUPLICATE arrival of the
    same request_id never calls `compute()` again — it waits for the
    original to finish and returns the exact same result, so a double-click
    or a client-side retry never dispatches a second paid model call. A
    `compute()` failure still unblocks any waiting duplicate (with `None`,
    surfaced as a 409) rather than leaving it hanging until the registry's
    TTL sweep.
    """
    entry, is_new = request_registry.begin(request_id)
    if not is_new:
        result = request_registry.wait_for_result(entry)  # type: ignore[arg-type]
        if result is None:
            raise HTTPException(
                status_code=409,
                detail="Timed out waiting for the original request to finish.",
            )
        return result
    try:
        result = compute()
    except Exception:
        request_registry.finish(entry, None)
        raise
    request_registry.finish(entry, result)
    return result


@router.post("/v1/requests/{request_id}/cancel")
def cancel_request(request_id: str):
    """Explicit abort — the Stop button's cancellation signal, distinct
    from a bare client disconnect (see app/request_registry.py's module
    docstring and _stream_and_persist's for the full rationale). Marks the
    matching in-flight worker's entry so it stops itself at its next
    check — between provider-stream events for a streaming ask/regenerate/
    edit, or not at all for a non-streaming call (see _dedup_or_call; a
    non-streaming ask has no natural checkpoint to abort at mid-call, so
    this only ever affects the streaming endpoints in practice).

    No owner check: `request_id` is an unguessable, single-use, short-lived
    UUID the client itself generated and is the only party who could ever
    know it — the same trust boundary a share-link token already relies on
    (see app/routers/shares.py), not a resource needing per-owner ACLs.
    Returns `{"cancelled": bool}` — False for an unknown or already-finished
    request_id, never an error (cancelling something that's already done is
    a no-op, not a mistake worth failing loudly over).
    """
    return {"cancelled": request_registry.mark_aborted(request_id)}


@router.get("/v1/bookmarks", response_model=list[BookmarkedMessage])
def bookmarks(owner: str | None = Depends(current_owner)):
    """Every bookmarked message across this owner's conversations, newest
    first, so a bookmark set on any one conversation is reviewable in one
    place instead of only visible while that conversation happens to be
    open.
    """
    return list_bookmarked_messages(owner)


@router.get(
    "/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
def conversation_messages(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    _owned_or_404(conversation_id, owner)
    return list_messages(conversation_id)


@router.delete("/v1/conversations/{conversation_id}/messages/{message_id}")
def remove_message(
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Delete a single message (either role) without touching any other
    message — distinct from regenerate/edit, which both replace or discard
    a range of messages and produce a fresh answer."""
    _owned_or_404(conversation_id, owner)
    deleted = delete_message(conversation_id, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"status": "deleted", "message_id": message_id}


@router.post(
    "/v1/conversations/{conversation_id}/messages/restore",
    response_model=MessageOut,
)
def restore_message(
    conversation_id: int,
    req: MessageRestoreRequest,
    owner: str | None = Depends(current_owner),
):
    """Recreate a single message (fresh id, no model call) in this
    conversation — the backing endpoint for Undo after deleting a message.
    Same fidelity as Import, attachments included."""
    _owned_or_404(conversation_id, owner)
    return add_message(
        conversation_id=conversation_id,
        role=req.role,
        content=req.content,
        mode_used=req.mode_used,
        notes=req.notes,
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        cost_usd=req.cost_usd,
        cached=req.cached,
        sources=_encode_sources(req.sources),
        truncated=req.truncated,
        code_results=_encode_code_results(req.code_results),
        fact_checks=_encode_fact_checks(req.fact_checks),
        academic_results=_encode_academic_results(req.academic_results),
        math_results=_encode_math_results(req.math_results),
        library_sources=_encode_library_sources(req.library_sources),
        workflow_steps=_encode_workflow_steps(req.workflow_steps),
        images=_encode_images(req.images),
        files=_encode_files(req.files),
        audio=_encode_audio(req.audio),
        model=req.model,
        feedback=req.feedback,
        feedback_reason=req.feedback_reason,
    )


@router.put(
    "/v1/conversations/{conversation_id}/messages/{message_id}/bookmark",
    response_model=MessageOut,
)
def bookmark_message(
    conversation_id: int,
    message_id: int,
    req: MessageBookmark,
    owner: str | None = Depends(current_owner),
):
    """Bookmark/unbookmark a single message — a marker on one turn, distinct
    from favoriting the whole conversation."""
    _owned_or_404(conversation_id, owner)
    updated = set_message_bookmarked(conversation_id, message_id, req.bookmarked)
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


_FEEDBACK_VERDICTS = {"up": 1, "down": -1}


@router.put(
    "/v1/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageOut,
)
def rate_message(
    conversation_id: int,
    message_id: int,
    req: MessageFeedback,
    owner: str | None = Depends(current_owner),
):
    """Rate/clear a caller's 👍/👎 on one assistant answer — the quality
    signal that closes the loop on this app's cost-only routing metrics
    (see app/feedback.py). Assistant messages only: rating a user's own
    turn makes no sense, so that's a 422, not a silent no-op. Setting the
    SAME verdict already recorded clears it instead (see
    database.set_message_feedback) — the click-again-to-clear contract the
    UI relies on, enforced here so a direct API call gets it too."""
    _owned_or_404(conversation_id, owner)

    message = get_message(message_id)
    if message is None or int(message["conversation_id"]) != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message["role"] != "assistant":
        raise HTTPException(
            status_code=422, detail="Only assistant messages can be rated."
        )

    verdict = _FEEDBACK_VERDICTS.get(req.verdict) if req.verdict else None
    updated = set_message_feedback(
        conversation_id, message_id, owner, verdict, req.reason
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


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

    result = run_orchestrator(contextual_req, owner=owner)

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

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    # Transcribe any attached audio BEFORE anything else touches req.files —
    # everything downstream (persistence, context building, the model call
    # itself) reads req.files, so folding the transcript in here is the one
    # place that needs to know audio was ever involved. See
    # app/audio_ingestion.py.
    req.files, audio_meta = resolve_audio_attachments(req.audio, req.files, owner)

    add_message(
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
        result = run_workflow(req, owner=owner)
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
            add_message(
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
    result = run_orchestrator(
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
        add_message(
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

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    # See _ask_conversation_impl's identical step for why this runs before
    # req.files is read by anything else.
    req.files, audio_meta = resolve_audio_attachments(req.audio, req.files, owner)

    add_message(
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


_QUEUE_DONE = object()


def _run_workflow_stream_worker(
    workflow_stream: Generator[dict[str, Any], None, None],
    events: "queue.Queue[object]",
    entry: request_registry._Entry | None,
    conversation_id: int,
    context_note: str,
) -> None:
    """Workflow-mode equivalent of _run_ask_stream_worker — see that
    function's docstring and _stream_and_persist's module note for the full
    disconnect-proofing rationale; the only real difference here is the
    persisted shape (workflow_steps instead of sources/pending_action/etc)."""
    accumulated: list[str] = []
    mode_used = "workflow"
    meta_event: tuple[str, dict[str, Any]] | None = None
    final_event: tuple[str, dict[str, Any]] = ("error", {"message": "no answer"})

    try:
        for event in workflow_stream:
            name = str(event["event"])
            data = dict(event["data"])

            if name == "meta":
                mode_used = str(data.get("mode_used", mode_used))
                meta_event = (name, data)

            elif name == "delta":
                accumulated.append(str(data.get("text", "")))

            elif name == "done":
                answer = str(data.get("answer", ""))
                mode_used = str(data.get("mode_used", mode_used))
                if answer.strip():
                    data["notes"] = f"{data.get('notes', '')} | {context_note}"
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=answer,
                        mode_used=mode_used,
                        notes=str(data["notes"]),
                        input_tokens=data.get("input_tokens"),
                        output_tokens=data.get("output_tokens"),
                        cost_usd=data.get("cost_usd"),
                        workflow_steps=json.dumps(data["workflow_steps"])
                        if data.get("workflow_steps")
                        else None,
                    )
                else:
                    # Same "never write an empty bubble" guard as the
                    # ordinary ask path — see _run_ask_stream_worker.
                    data["notes"] = (
                        f"{data.get('notes', '')} | {context_note} "
                        "| not saved (empty answer)"
                    )
                final_event = (name, data)

            elif name == "error":
                partial = "".join(accumulated).strip()
                if partial:
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=partial,
                        mode_used=mode_used,
                        notes=(
                            f"Interrupted before completion: "
                            f"{data.get('message', '')} | {context_note}"
                        ),
                    )
                final_event = (name, data)

            events.put((name, data))

            # Explicit abort only — see _run_ask_stream_worker's identical
            # comment. A disconnect between workflow steps never sets this
            # flag, so the workflow keeps running its remaining steps to
            # completion and persists as normal.
            if request_registry.is_aborted(entry):
                workflow_stream.close()
                partial = "".join(accumulated).strip()
                if partial:
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=partial,
                        mode_used=mode_used,
                        notes=f"Cancelled by user | {context_note}",
                    )
                cancelled_data = {"message": "Cancelled by user"}
                final_event = ("error", cancelled_data)
                events.put(("error", cancelled_data))
                break
    except Exception:  # pragma: no cover - defense in depth, see logger.exception
        logger.exception(
            "stream.workflow_worker_failed conversation_id=%s", conversation_id
        )
        error_data = {"message": "Internal error"}
        final_event = ("error", error_data)
        events.put(("error", error_data))
    finally:
        request_registry.finish(entry, {"meta": meta_event, "final": final_event})
        events.put(_QUEUE_DONE)


def _stream_workflow_and_persist(
    conversation_id: int,
    req: AskRequest,
    context_note: str,
    owner: str | None = None,
) -> StreamingResponse:
    """Stream an opt-in workflow answer (see app/workflow.py) as SSE and
    persist the assistant message with its workflow_steps breakdown.

    A separate helper from _stream_and_persist rather than a branch inside
    it: the event set is different (an extra "step" event alongside meta/
    delta/done/error) and workflow mode never threads
    cacheable_system/context_free/memory — see ask_conversation_stream's
    workflow branch, which calls this instead of _stream_and_persist for
    the exact same reason its non-streaming sibling calls run_workflow
    instead of run_orchestrator directly. Same disconnect-proof-generation
    and idempotency design as _stream_and_persist — see that function's
    module docstring for the full rationale; kept in a separate worker
    (_run_workflow_stream_worker) rather than sharing one, matching the
    existing "separate helper, different event/persistence shape" split.
    """
    entry, is_new = request_registry.begin(req.request_id)

    if not is_new:
        result = request_registry.wait_for_result(entry)  # type: ignore[arg-type]
        return StreamingResponse(
            _replay_duplicate_stream(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    workflow_stream = stream_workflow(req, owner=owner)
    events: "queue.Queue[object]" = queue.Queue()
    worker = threading.Thread(
        target=_run_workflow_stream_worker,
        args=(workflow_stream, events, entry, conversation_id, context_note),
        daemon=True,
    )
    worker.start()

    def event_stream() -> Iterator[str]:
        while True:
            item = events.get()
            if item is _QUEUE_DONE:
                return
            name, data = cast("tuple[str, dict[str, Any]]", item)
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _run_ask_stream_worker(
    orchestrator_stream: Generator[dict[str, Any], None, None],
    events: "queue.Queue[object]",
    entry: request_registry._Entry | None,
    conversation_id: int,
    context_note: str,
    replace_after_id: int | None,
    edit_message_id: int | None,
    edit_question: str | None,
    edit_images: list[str] | None,
    edit_files: list[FileAttachment] | None,
    remember_memory: bool,
    memory_question: str | None,
    memory_vector: list[float] | None,
    owner: str | None,
) -> None:
    """The actual generation+persistence work, run on its own thread — see
    the module docstring's "DISCONNECT-PROOF GENERATION" note above
    _stream_and_persist for why this is a plain thread rather than living
    inside the SSE-facing generator.

    Consumes `orchestrator_stream` to completion (or until an explicit abort
    is flagged on `entry` — see request_registry) regardless of whether
    anything is still reading `events`; the CALLER (the SSE generator) may
    stop draining `events` at any point without this loop ever knowing or
    caring. Every (event_name, data) pair is put on `events` so a still-
    connected client keeps seeing live deltas exactly as before; the final
    (name, data) — always either ("done", ...) or ("error", ...) — is also
    published to `entry` via request_registry.finish so a duplicate
    request_id arriving later gets the same result without a second call.
    """
    accumulated: list[str] = []
    mode_used = "unknown"
    meta_event: tuple[str, dict[str, Any]] | None = None
    final_event: tuple[str, dict[str, Any]] = ("error", {"message": "no answer"})

    try:
        for event in orchestrator_stream:
            name = str(event["event"])
            data = dict(event["data"])

            if name == "meta":
                mode_used = str(data.get("mode_used", mode_used))
                meta_event = (name, data)

            elif name == "delta":
                accumulated.append(str(data.get("text", "")))

            elif name == "done":
                answer = str(data.get("answer", ""))
                mode_used = str(data.get("mode_used", mode_used))
                if answer.strip():
                    data["notes"] = f"{data.get('notes', '')} | {context_note}"
                    # Replace-in-place happens here (not up front), so the old
                    # message(s) survive any earlier failure. Persisted before
                    # the terminal frame so clients can refetch on "done".
                    if edit_message_id is not None:
                        delete_messages_from(conversation_id, edit_message_id)
                        add_message(
                            conversation_id=conversation_id,
                            role="user",
                            content=edit_question or "",
                            images=_encode_images(edit_images),
                            files=_encode_files(edit_files),
                        )
                    elif replace_after_id is not None:
                        delete_messages_after(conversation_id, replace_after_id)
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=answer,
                        mode_used=mode_used,
                        notes=str(data["notes"]),
                        input_tokens=data.get("input_tokens"),
                        output_tokens=data.get("output_tokens"),
                        cost_usd=data.get("cost_usd"),
                        cached=bool(data.get("cached", False)),
                        sources=json.dumps(data["sources"])
                        if data.get("sources")
                        else None,
                        pending_action=json.dumps(data["pending_action"])
                        if data.get("pending_action")
                        else None,
                        action_status="pending" if data.get("pending_action") else None,
                        images=json.dumps(data["images"])
                        if data.get("images")
                        else None,
                        truncated=bool(data.get("truncated", False)),
                        code_results=json.dumps(data["code_results"])
                        if data.get("code_results")
                        else None,
                        fact_checks=json.dumps(data["fact_checks"])
                        if data.get("fact_checks")
                        else None,
                        academic_results=json.dumps(data["academic_results"])
                        if data.get("academic_results")
                        else None,
                        model=data.get("model"),
                        math_results=json.dumps(data["math_results"])
                        if data.get("math_results")
                        else None,
                        library_sources=json.dumps(data["library_sources"])
                        if data.get("library_sources")
                        else None,
                    )
                    if remember_memory:
                        memory.remember(
                            owner,
                            conversation_id,
                            memory_question or "",
                            answer,
                            memory_vector,
                        )
                else:
                    # Empty 'done' (model returned nothing, or a reasoning call
                    # truncated before any output): keep history as-is — never
                    # blank a good prior answer on regenerate, nor write an empty
                    # bubble on ask — and tell the client nothing was saved.
                    #
                    # A truncated reasoning call can be empty yet costly. It is
                    # intentionally not stored as a message (an empty row purely
                    # to carry cost would reintroduce the pollution this guard
                    # prevents), but its cost is NOT lost: stream_orchestrator
                    # records it to the spend_log, so the daily budget still sees
                    # it. The client is also told here that nothing was saved.
                    data["notes"] = (
                        f"{data.get('notes', '')} | {context_note} "
                        "| not saved (empty answer)"
                    )
                final_event = (name, data)

            elif name == "error":
                # A regeneration or edit that fails keeps the existing message(s)
                # and discards the partial; a normal ask persists whatever streamed.
                partial = "".join(accumulated).strip()
                if replace_after_id is None and edit_message_id is None and partial:
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=partial,
                        mode_used=mode_used,
                        notes=(
                            f"Interrupted before completion: "
                            f"{data.get('message', '')} | {context_note}"
                        ),
                    )
                final_event = (name, data)

            events.put((name, data))

            # Explicit abort (the Stop button — see request_registry's
            # module docstring): the ONLY way this loop stops before
            # orchestrator_stream is naturally exhausted. A bare client
            # disconnect never reaches here — nothing about this thread's
            # lifecycle is tied to whether anyone is still draining
            # `events`, which is exactly the fix (see the module note
            # above _stream_and_persist on the verified disconnect-
            # propagation finding).
            if request_registry.is_aborted(entry):
                orchestrator_stream.close()
                partial = "".join(accumulated).strip()
                if replace_after_id is None and edit_message_id is None and partial:
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=partial,
                        mode_used=mode_used,
                        notes=f"Cancelled by user | {context_note}",
                    )
                cancelled_data = {"message": "Cancelled by user"}
                final_event = ("error", cancelled_data)
                events.put(("error", cancelled_data))
                break
    except Exception:  # pragma: no cover - defense in depth, see logger.exception
        logger.exception("stream.worker_failed conversation_id=%s", conversation_id)
        error_data = {"message": "Internal error"}
        final_event = ("error", error_data)
        events.put(("error", error_data))
    finally:
        request_registry.finish(entry, {"meta": meta_event, "final": final_event})
        events.put(_QUEUE_DONE)


def _replay_duplicate_stream(
    result: dict[str, object] | None,
) -> Iterator[str]:
    """SSE frames for a DUPLICATE request_id (see request_registry): no new
    model call, just a synthesized meta + final frame reusing the ORIGINAL
    caller's already-computed result. No delta frames — this caller never
    watched the tokens stream in, only the dedup guarantee (no second paid
    call, same final answer) matters here."""
    if result is None:
        # The original never called request_registry.finish in time (see
        # wait_for_result's timeout) — genuinely unexpected (that timeout is
        # far past any real answer's latency), surfaced as an error rather
        # than hanging the duplicate caller forever.
        yield (
            "event: error\n"
            f"data: {json.dumps({'message': 'Timed out waiting for the original request to finish.'})}\n\n"
        )
        return
    meta = result.get("meta")
    if meta is not None:
        meta_name, meta_data = cast("tuple[str, dict[str, Any]]", meta)
        yield f"event: {meta_name}\ndata: {json.dumps(meta_data)}\n\n"
    final_name, final_data = cast("tuple[str, dict[str, Any]]", result["final"])
    yield f"event: {final_name}\ndata: {json.dumps(final_data)}\n\n"


def _stream_and_persist(
    conversation_id: int,
    contextual_req: AskRequest,
    context_note: str,
    replace_after_id: int | None = None,
    routing_question: str | None = None,
    owner: str | None = None,
    edit_message_id: int | None = None,
    edit_question: str | None = None,
    edit_images: list[str] | None = None,
    edit_files: list[FileAttachment] | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
    context_free: bool = False,
    remember_memory: bool = False,
    memory_question: str | None = None,
    memory_vector: list[float] | None = None,
    pre_stage_timings: dict[str, int] | None = None,
    library_sources: list[dict] | None = None,
    request_id: str | None = None,
) -> StreamingResponse:
    """Stream an orchestrator response as SSE and persist the assistant message.

    Shared by the ask-stream, regenerate-stream, and edit-stream endpoints.
    When `replace_after_id` is set (regenerate), the previous answer(s) after
    that message are deleted only on a successful `done` — right before the
    new answer is stored — so a failed or aborted regeneration leaves the old
    answer intact. `edit_message_id` (edit) works the same way but ALSO
    replaces the edited user message itself: on success, that message and
    everything after it is deleted and a fresh user message (`edit_question`/
    `edit_images`/`edit_files`) is persisted before the new answer — a failed
    or aborted edit leaves the original message and its answer untouched.

    `cacheable_system`/`anthropic_question` are only ever populated by the
    ask-stream endpoint (the one call site with a stable per-conversation
    checkpoint to isolate — see build_context_prompt_with_cache_split);
    regenerate-stream and edit-stream leave both None and get today's
    behavior unchanged. `context_free` (see run_orchestrator's docstring)
    defaults False for the same reason: only the ask-stream call site has
    verified there's no history/system-prompt behind the question.

    `remember_memory`/`memory_question`/`memory_vector` (see app/memory.py)
    are likewise only ever set by ask-stream — cross-conversation memory is
    scoped to genuinely new turns, not a regenerated or edited answer to one
    already remembered.

    `pre_stage_timings` (see telemetry.StageTimer) is threaded straight to
    stream_orchestrator for the per-stage latency log — see
    _memory_stage_timing.

    `library_sources` (see app/rag_library.py's summarize_sources) is
    likewise only ever populated by ask-stream — precomputed by
    _recall_library before this call, threaded straight to
    stream_orchestrator so the "done" event (and, on success, the persisted
    message) can carry it. Never set on regenerate-stream/edit-stream, same
    reasoning as remember_memory.

    DISCONNECT-PROOF GENERATION: verified finding (see CHANGELOG's
    Unreleased entry for the full writeup) — with modern uvicorn/Starlette
    (ASGI spec_version >= 2.4), a client disconnect is detected ONLY as an
    OSError the next time this response tries to `send()` a chunk to the
    now-closed socket; there is no separate "disconnect listener" racing
    the stream and cancelling it. Critically, that means a disconnect
    mid-generation — while this thread is blocked deep inside a
    synchronous provider SDK call waiting on the NEXT token — does NOT
    raise GeneratorExit into the generator at all (GeneratorExit only
    reaches a generator that is actually suspended AT a `yield`, which a
    thread blocked inside a blocking I/O call is not). Concretely: the
    previous implementation's `except GeneratorExit: orchestrator_stream
    .close()` handler was live code, but in the realistic "disconnect
    happens while the model is mid-answer" case it mostly never fired —
    the model call kept running in its orphaned thread, paid tokens still
    billed, but with nothing left listening to persist the result. Only
    the narrow race window between two already-produced SSE events (which
    is what the pre-existing test simulated, by construction) reliably hit
    that handler.

    The fix: `_run_ask_stream_worker` above runs on its OWN thread,
    started before this function returns, with no dependency on whether
    Starlette is still consuming this response at all. `event_stream`
    below is a thin, passive reader of that worker's output queue — if the
    client goes away, Starlette simply stops calling `next()` on
    `event_stream`, which stops reading the queue, which has zero effect
    on the worker: it keeps calling `add_message`/recording spend exactly
    as it would have with a client still attached, and the client finds
    the finished answer on reconnect (refetching the conversation).
    Existing per-call timeouts and token caps still bound the worker the
    same way they always bounded a normal request — nothing here removes
    a limit, it only decouples "is anyone watching" from "does the work
    finish."

    EXPLICIT ABORT stays a real abort: `request_id` (an idempotency key —
    see request_registry) is also the STOP BUTTON's cancellation handle.
    POST /v1/requests/{request_id}/cancel flags request_registry's entry;
    `_run_ask_stream_worker` checks that flag between provider events and,
    if set, closes `orchestrator_stream` itself — the same
    GeneratorExit-based reservation-release stream_orchestrator already
    does today, just triggered deliberately instead of by an ambiguous
    disconnect. A bare disconnect with no matching cancel call never sets
    this flag, so the worker just keeps going — the whole point.

    IDEMPOTENCY: a duplicate arrival of the same `request_id` (double-click,
    a client retry) never starts a second `_run_ask_stream_worker` — see
    the `is_new` branch below, which instead replays the ORIGINAL caller's
    result via `_replay_duplicate_stream` once it's ready.
    """
    entry, is_new = request_registry.begin(request_id)

    if not is_new:
        result = request_registry.wait_for_result(entry)  # type: ignore[arg-type]
        return StreamingResponse(
            _replay_duplicate_stream(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    orchestrator_stream = stream_orchestrator(
        contextual_req,
        routing_question,
        owner,
        history=history,
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=context_free,
        pre_stage_timings=pre_stage_timings,
        library_sources=library_sources,
    )
    events: "queue.Queue[object]" = queue.Queue()
    worker = threading.Thread(
        target=_run_ask_stream_worker,
        args=(
            orchestrator_stream,
            events,
            entry,
            conversation_id,
            context_note,
            replace_after_id,
            edit_message_id,
            edit_question,
            edit_images,
            edit_files,
            remember_memory,
            memory_question,
            memory_vector,
            owner,
        ),
        daemon=True,
    )
    worker.start()

    def event_stream() -> Iterator[str]:
        # Passive reader ONLY — see the docstring above. Nothing in this
        # generator's closure (including its own GeneratorExit, should
        # Starlette abandon it) reaches into the worker thread or
        # orchestrator_stream; the worker owns their entire lifecycle.
        while True:
            item = events.get()
            if item is _QUEUE_DONE:
                return
            name, data = cast("tuple[str, dict[str, Any]]", item)
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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

    result = run_orchestrator(
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
        add_message(
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

    result = run_orchestrator(
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
        delete_messages_from(conversation_id, message_id)
        add_message(
            conversation_id=conversation_id,
            role="user",
            content=req.question,
            images=_encode_images(req.images),
            files=_encode_files(req.files),
        )
        add_message(
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


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/action",
    response_model=ActionResult,
)
def resolve_action(
    conversation_id: int,
    message_id: int,
    req: ActionConfirmRequest,
    owner: str | None = Depends(current_owner),
):
    """Confirm or decline a message's proposed action (propose-then-confirm).

    Nothing is ever fired automatically by the orchestrator — this endpoint is
    the ONLY path that can trigger the webhook, and only on an explicit
    confirm=true from the caller.
    """
    _owned_or_404(conversation_id, owner)

    message = get_message(message_id)
    if message is None or int(message["conversation_id"]) != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.get("action_status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Action already resolved (status={message.get('action_status')!r}).",
        )

    if not req.confirm:
        claimed = claim_pending_action(message_id, "declined")
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="Action already resolved by a concurrent request.",
            )
        return ActionResult(action_status=str(claimed["action_status"]))

    # Claim the action atomically before firing the webhook, so two concurrent
    # confirm requests can't both pass the pending-check above and both post.
    # Only the request whose UPDATE actually matches the still-pending row
    # wins the claim; the loser gets a 409 instead of double-firing.
    claimed = claim_pending_action(message_id, "confirmed")
    if claimed is None:
        raise HTTPException(
            status_code=409, detail="Action already resolved by a concurrent request."
        )

    stored_action = json.loads(str(message["pending_action"]))
    action_name = str(stored_action.get("action", ""))
    payload = stored_action.get("payload", {})
    success, detail = post_webhook(action_name, payload)
    if not success:
        updated = set_action_status(message_id, "failed")
        assert updated is not None
        return ActionResult(action_status=str(updated["action_status"]), detail=detail)
    return ActionResult(action_status=str(claimed["action_status"]), detail=detail)
