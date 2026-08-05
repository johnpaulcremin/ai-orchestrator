"""Shared dedup/streaming engine used by the ask/regenerate/edit route
families (see messages/ask.py, messages/regenerate.py, messages/edit.py) —
split out of the original single messages.py purely because this is the one
piece all three genuinely share, not owned by any single route family.

See app/routers/messages/__init__.py's module docstring for why
`stream_orchestrator`/`stream_workflow` are read via `_messages.<name>`
rather than a bare imported name — required for
`monkeypatch.setattr(app.routers.messages, "stream_orchestrator", fake)` to
keep affecting this module's calls after the split.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Generator, Iterator
from typing import Any, cast

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

import app.routers.messages as _messages
from ... import memory, request_registry
from ...database import add_message, delete_messages_after, delete_messages_from
from ...schemas import AskRequest, FileAttachment
from ...telemetry import logger
from ..deps import _encode_files, _encode_images


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

    workflow_stream = _messages.stream_workflow(req, owner=owner)
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
                        search_queries=json.dumps(data["search_queries"])
                        if data.get("search_queries")
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
                        memory_sources=json.dumps(data["memory_sources"])
                        if data.get("memory_sources")
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
    memory_sources: list[dict] | None = None,
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
    reasoning as remember_memory. `memory_sources` (app/memory.py's
    summarize_sources) is threaded the identical way.

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

    orchestrator_stream = _messages.stream_orchestrator(
        contextual_req,
        routing_question,
        owner,
        history=history,
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=context_free,
        pre_stage_timings=pre_stage_timings,
        library_sources=library_sources,
        memory_sources=memory_sources,
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
