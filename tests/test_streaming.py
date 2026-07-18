from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main
from app.schemas import AskRequest

SSEEvent = dict[str, Any]


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, events: list[SSEEvent]
) -> list[AskRequest]:
    """Replace stream_orchestrator with a scripted generator; record requests."""
    calls: list[AskRequest] = []

    def fake_stream_orchestrator(req: AskRequest) -> Iterator[SSEEvent]:
        calls.append(req)
        yield from events

    monkeypatch.setattr(app.main, "stream_orchestrator", fake_stream_orchestrator)
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
