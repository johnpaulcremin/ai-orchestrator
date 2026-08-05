from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app import request_registry
from app.schemas import AskRequest, Mode

SSEEvent = dict[str, Any]


@pytest.fixture(autouse=True)
def _clean_request_registry():
    """Every test in this module gets a clean idempotency registry — a
    request_id (or the untracked-request "no request_id" case, since begin()
    treats every falsy request_id as independently new) must never leak
    state between tests."""
    request_registry._reset_for_tests()
    yield
    request_registry._reset_for_tests()


def _wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll `predicate` until it's truthy or `timeout` elapses — the
    disconnect-proof-generation tests below start a REAL background thread
    (see app.routers.messages._run_ask_stream_worker) whose completion isn't
    otherwise observable from the test's own thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, events: list[SSEEvent]
) -> list[AskRequest]:
    """Replace stream_orchestrator with a scripted generator; record requests."""
    calls: list[AskRequest] = []

    def fake_stream_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        library_sources: list[dict] | None = None,
        memory_sources: list[dict] | None = None,
    ) -> Iterator[SSEEvent]:
        calls.append(req)
        yield from events

    monkeypatch.setattr(
        app.routers.messages, "stream_orchestrator", fake_stream_orchestrator
    )
    return calls


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []

    for block in body.strip().split("\n\n"):
        event_name = ""
        data_raw = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_raw = line[len("data: ") :]
        frames.append((event_name, json.loads(data_raw)))

    return frames


def _create_conversation(client: TestClient, title: str) -> int:
    response = client.post("/v1/conversations", json={"title": title})
    assert response.status_code == 200
    return int(response.json()["id"])


def test_stream_success_frames_and_persistence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_data = {
        "request_id": "req-1",
        "mode_used": "auto->fast",
        "model": "fast-model-x",
        "notes": "scripted routing",
    }
    events: list[SSEEvent] = [
        {"event": "meta", "data": dict(meta_data)},
        {"event": "delta", "data": {"text": "Hello "}},
        {"event": "delta", "data": {"text": "world."}},
        {
            "event": "done",
            "data": {
                "answer": "Hello world.",
                "mode_used": "auto->fast",
                "notes": "scripted notes",
            },
        },
    ]
    calls = _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Stream test")
    question = "Say hello"

    response = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={"question": question, "mode": "auto"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(response.text)
    assert [name for name, _ in frames] == ["meta", "delta", "delta", "done"]

    assert frames[0][1] == meta_data
    assert frames[1][1] == {"text": "Hello "}
    assert frames[2][1] == {"text": "world."}
    assert frames[3][1] == {
        "answer": "Hello world.",
        "mode_used": "auto->fast",
        "notes": "scripted notes | context_messages=0",
    }

    # No prior history, so the orchestrator saw the bare question.
    assert len(calls) == 1
    assert calls[0].question == question

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert len(messages) == 2

    user_message, assistant_message = messages
    assert user_message["role"] == "user"
    assert user_message["content"] == question

    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "Hello world."
    assert assistant_message["mode_used"] == "auto->fast"
    assert assistant_message["notes"] == "scripted notes | context_messages=0"


def test_stream_error_after_partial_persists_partial(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-2",
                "mode_used": "auto->smart",
                "model": "smart-model-y",
                "notes": "scripted routing",
            },
        },
        {"event": "delta", "data": {"text": "Partial answer "}},
        {"event": "error", "data": {"message": "boom"}},
    ]
    _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Stream error test")
    question = "This will fail midway"

    response = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={"question": question},
    )

    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [name for name, _ in frames] == ["meta", "delta", "error"]
    assert frames[2][1] == {"message": "boom"}

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert len(messages) == 2

    user_message, assistant_message = messages
    assert user_message["role"] == "user"
    assert user_message["content"] == question

    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "Partial answer"
    assert assistant_message["mode_used"] == "auto->smart"
    assert (
        assistant_message["notes"]
        == "Interrupted before completion: boom | context_messages=0"
    )


def test_stream_error_before_output_persists_nothing_extra(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-3",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        },
        {"event": "error", "data": {"message": "no output at all"}},
    ]
    _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Stream early error")
    question = "This fails before any text"

    response = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={"question": question},
    )

    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert [name for name, _ in frames] == ["meta", "error"]
    assert frames[1][1] == {"message": "no output at all"}

    # Only the user message was persisted.
    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == question


def test_client_disconnect_mid_stream_still_completes_and_persists_the_full_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DISCONNECT-PROOF GENERATION (see _stream_and_persist's module
    docstring for the full verified-finding writeup): a Stop click / tab
    close / dropped connection mid-stream must NOT stop the underlying
    model call — it keeps running to completion on its own background
    thread and persists the FULL answer, exactly as if nobody had
    disconnected. The client finds the finished answer by refetching the
    conversation on reconnect, same as this test does.

    TestClient's `.post()` always consumes a StreamingResponse to completion,
    so it can't reproduce a real client disconnect; this drives the response's
    body_iterator directly and closes it early, exactly as Starlette does when
    an HTTP client disconnects.
    """
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-x",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        },
        {"event": "delta", "data": {"text": "Partial "}},
        {"event": "delta", "data": {"text": "answer here."}},
        {
            "event": "done",
            "data": {
                "answer": "Partial answer here.",
                "mode_used": "auto->fast",
                "notes": "n",
            },
        },
    ]
    calls = _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Disconnect test")
    response = app.routers.messages._stream_and_persist(
        conversation_id,
        AskRequest(question="Say something long", mode=Mode.auto),
        "context_messages=0",
    )

    # StreamingResponse wraps our sync generator in Starlette's
    # iterate_in_threadpool, turning body_iterator into an async iterator —
    # drive it with asyncio and close it early, the same way Starlette
    # abandons/closes it when an HTTP client disconnects mid-response.
    async def drive_and_disconnect() -> None:
        agen = response.body_iterator
        await agen.__anext__()  # meta
        await agen.__anext__()  # first delta
        await agen.aclose()

    asyncio.run(drive_and_disconnect())

    def _has_assistant_message() -> bool:
        rows = client.get(f"/v1/conversations/{conversation_id}/messages").json()
        return any(m["role"] == "assistant" for m in rows)

    _wait_for(_has_assistant_message)

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    # The FULL answer, not the two chunks that had streamed before the
    # simulated disconnect — the worker kept consuming orchestrator_stream
    # to its natural "done" event regardless of the abandoned SSE consumer.
    assert assistant_messages[0]["content"] == "Partial answer here."
    assert "client disconnected" not in assistant_messages[0]["notes"]

    # Exactly ONE model call — the disconnect never triggered a retry or a
    # second dispatch of any kind.
    assert len(calls) == 1


def test_disconnect_reconciles_spend_via_the_normal_done_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    """The worker's persistence path is the SAME "done" handling a normal,
    fully-connected request goes through — so spend recording (done inside
    stream_orchestrator itself, upstream of _run_ask_stream_worker) is
    equally unaffected by the disconnect. This asserts the assistant
    message's cost fields actually landed, the concrete signal that the
    full completion+persistence path ran, not just a fallback partial-save.
    """
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-spend",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        },
        {"event": "delta", "data": {"text": "Full answer."}},
        {
            "event": "done",
            "data": {
                "answer": "Full answer.",
                "mode_used": "auto->fast",
                "notes": "n",
                "input_tokens": 42,
                "output_tokens": 7,
                "cost_usd": 0.0123,
            },
        },
    ]
    _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Disconnect spend test")
    response = app.routers.messages._stream_and_persist(
        conversation_id,
        AskRequest(question="Say something", mode=Mode.auto),
        "context_messages=0",
    )

    async def drive_and_disconnect() -> None:
        agen = response.body_iterator
        await agen.__anext__()  # meta
        await agen.aclose()

    asyncio.run(drive_and_disconnect())

    def _has_assistant_message() -> bool:
        rows = client.get(f"/v1/conversations/{conversation_id}/messages").json()
        return any(m["role"] == "assistant" for m in rows)

    _wait_for(_has_assistant_message)

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assistant_message = next(m for m in messages if m["role"] == "assistant")
    assert assistant_message["content"] == "Full answer."
    assert assistant_message["cost_usd"] == pytest.approx(0.0123)
    assert assistant_message["input_tokens"] == 42
    assert assistant_message["output_tokens"] == 7


def test_explicit_abort_cancels_and_is_distinct_from_a_disconnect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Stop button (POST /v1/requests/{request_id}/cancel) is a genuine
    abort: the worker stops consuming orchestrator_stream early, closes it
    (triggering stream_orchestrator's own reservation-release GeneratorExit
    handling), and persists only the partial text with a "Cancelled by
    user" note — never the full answer a bare disconnect now completes."""

    def fake_stream_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        library_sources: list[dict] | None = None,
        memory_sources: list[dict] | None = None,
    ) -> Iterator[SSEEvent]:
        yield {
            "event": "meta",
            "data": {
                "request_id": "req-abort",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        }
        # A real cancel arrives here, between two events. Generator
        # resumption timing matters: this runs when the CONSUMER asks for
        # the NEXT item after already having received+queued "meta", so by
        # the time the consumer processes the delta this produces and then
        # checks is_aborted() (see _run_ask_stream_worker), the flag is
        # already set — the loop queues this one delta, then breaks before
        # ever asking the generator for the events below.
        request_registry.mark_aborted("req-abort-key")
        yield {"event": "delta", "data": {"text": "Partial "}}
        yield {"event": "delta", "data": {"text": "answer that should never arrive."}}
        yield {
            "event": "done",
            "data": {"answer": "Full answer.", "mode_used": "auto->fast", "notes": "n"},
        }

    monkeypatch.setattr(
        app.routers.messages, "stream_orchestrator", fake_stream_orchestrator
    )

    conversation_id = _create_conversation(client, "Abort test")
    response = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={
            "question": "Say something",
            "mode": "auto",
            "request_id": "req-abort-key",
        },
    )

    assert response.status_code == 200
    frames = _parse_sse(response.text)
    # The worker breaks out right after the abort flag is observed, so the
    # stream ends with a synthesized "Cancelled by user" error frame instead
    # of ever reaching the scripted "done".
    assert [name for name, _ in frames] == ["meta", "delta", "error"]
    assert frames[2][1] == {"message": "Cancelled by user"}

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assistant_messages = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "Partial"
    assert "Cancelled by user" in assistant_messages[0]["notes"]


def test_disconnect_without_a_cancel_call_never_aborts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative case proving abort really is opt-in: the SAME scripted
    stream as the cancellation test above, but with no matching
    request_registry.mark_aborted call — the worker must run every event to
    a normal "done", regardless of the disconnect this test also drives."""
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-no-abort",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        },
        {"event": "delta", "data": {"text": "Full "}},
        {"event": "delta", "data": {"text": "answer."}},
        {
            "event": "done",
            "data": {"answer": "Full answer.", "mode_used": "auto->fast", "notes": "n"},
        },
    ]
    _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "No-abort disconnect test")
    response = app.routers.messages._stream_and_persist(
        conversation_id,
        AskRequest(
            question="Say something", mode=Mode.auto, request_id="req-no-abort-key"
        ),
        "context_messages=0",
        request_id="req-no-abort-key",
    )

    async def drive_and_disconnect() -> None:
        agen = response.body_iterator
        await agen.__anext__()  # meta
        await agen.__anext__()  # first delta
        await agen.aclose()

    asyncio.run(drive_and_disconnect())

    def _has_assistant_message() -> bool:
        rows = client.get(f"/v1/conversations/{conversation_id}/messages").json()
        return any(m["role"] == "assistant" for m in rows)

    _wait_for(_has_assistant_message)

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assistant_message = next(m for m in messages if m["role"] == "assistant")
    assert assistant_message["content"] == "Full answer."
    assert "Cancelled" not in assistant_message["notes"]


def test_workflow_disconnect_mid_step_still_completes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKFLOW MODE gets the same disconnect-proof-generation treatment as
    the ordinary ask path (see _stream_workflow_and_persist, which shares
    _run_ask_stream_worker's design via its own _run_workflow_stream_worker):
    a disconnect between steps must not stop the remaining steps or the
    final synthesis — see the module note above _stream_and_persist for the
    full rationale."""
    events: list[SSEEvent] = [
        {"event": "meta", "data": {"mode_used": "workflow", "model": "gpt-5"}},
        {"event": "step", "data": {"category": "coding", "status": "running"}},
        {"event": "step", "data": {"category": "coding", "status": "ok"}},
        {"event": "delta", "data": {"text": "final "}},
        {"event": "delta", "data": {"text": "answer"}},
        {
            "event": "done",
            "data": {
                "answer": "final answer",
                "mode_used": "workflow",
                "notes": "Workflow: 1 step(s) + synthesis",
                "workflow_steps": [
                    {
                        "category": "coding",
                        "instruction": "write it",
                        "model": "gpt-5",
                        "status": "ok",
                        "answer": "final answer",
                    }
                ],
            },
        },
    ]

    def fake_stream_workflow(
        req: AskRequest, owner: str | None = None
    ) -> Iterator[SSEEvent]:
        yield from events

    monkeypatch.setattr(app.routers.messages, "stream_workflow", fake_stream_workflow)

    conversation_id = _create_conversation(client, "Workflow disconnect test")
    response = app.routers.messages._stream_workflow_and_persist(
        conversation_id,
        AskRequest(question="do a two-part task", mode=Mode.workflow),
        "context_messages=0",
    )

    async def drive_and_disconnect() -> None:
        agen = response.body_iterator
        await agen.__anext__()  # meta
        await agen.__anext__()  # step running
        await agen.aclose()

    asyncio.run(drive_and_disconnect())

    def _has_assistant_message() -> bool:
        rows = client.get(f"/v1/conversations/{conversation_id}/messages").json()
        return any(m["role"] == "assistant" for m in rows)

    _wait_for(_has_assistant_message)

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assistant_message = next(m for m in messages if m["role"] == "assistant")
    assert assistant_message["content"] == "final answer"
    assert assistant_message["workflow_steps"] is not None
    assert len(assistant_message["workflow_steps"]) == 1


# --- idempotency: duplicate request_id joins the original's result ---------------


def test_duplicate_request_id_dispatches_exactly_one_model_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core idempotency guarantee: a second arrival of the SAME
    request_id (double-click, a client-side retry) never starts a second
    background worker — see _stream_and_persist's `is_new` branch — it
    joins the original's already-computed result instead."""
    events: list[SSEEvent] = [
        {
            "event": "meta",
            "data": {
                "request_id": "req-dup",
                "mode_used": "auto->fast",
                "model": "fast-model-x",
                "notes": "scripted routing",
            },
        },
        {"event": "delta", "data": {"text": "The answer."}},
        {
            "event": "done",
            "data": {"answer": "The answer.", "mode_used": "auto->fast", "notes": "n"},
        },
    ]
    calls = _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Dedup test")
    shared_request_id = "dedup-key-1"

    first = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={
            "question": "Say something",
            "mode": "auto",
            "request_id": shared_request_id,
        },
    )
    assert first.status_code == 200
    first_frames = _parse_sse(first.text)
    assert [name for name, _ in first_frames] == ["meta", "delta", "done"]

    # A second POST with the SAME request_id — the client double-clicked, or
    # retried after a slow/ambiguous response. It must not add a second user
    # turn or a second answer.
    second = client.post(
        f"/v1/conversations/{conversation_id}/ask/stream",
        json={
            "question": "Say something",
            "mode": "auto",
            "request_id": shared_request_id,
        },
    )
    assert second.status_code == 200
    second_frames = _parse_sse(second.text)
    # Replayed from the registry: a meta frame plus the SAME final "done" —
    # no delta frames (see _replay_duplicate_stream's docstring).
    assert [name for name, _ in second_frames] == ["meta", "done"]
    assert second_frames[-1][1]["answer"] == "The answer."

    # Exactly ONE model call across both requests.
    assert len(calls) == 1


def test_different_request_ids_each_dispatch_their_own_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative case: two DIFFERENT request_ids are never deduped
    against each other, even for the same conversation and question."""
    events: list[SSEEvent] = [
        {"event": "meta", "data": {"mode_used": "auto->fast", "model": "fast-model-x"}},
        {
            "event": "done",
            "data": {"answer": "ok", "mode_used": "auto->fast", "notes": "n"},
        },
    ]
    calls = _install_stream(monkeypatch, events)

    conversation_id = _create_conversation(client, "Non-dedup test")

    for request_id in ("req-a", "req-b"):
        response = client.post(
            f"/v1/conversations/{conversation_id}/ask/stream",
            json={"question": "hi", "mode": "auto", "request_id": request_id},
        )
        assert response.status_code == 200

    assert len(calls) == 2


def test_stream_404_for_missing_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stream(monkeypatch, [])

    response = client.post(
        "/v1/conversations/999999/ask/stream",
        json={"question": "Hello?"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "Conversation not found"
    assert calls == []
