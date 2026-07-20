"""Empty/failed model answers must never destroy or pollute conversation history.

Three reachable cases with one root cause (an empty answer, persisted without a
guard): the non-streaming ask writing an empty assistant bubble, the streaming
ask doing the same, and — the worst — a streaming regenerate whose empty `done`
deletes the previous good answer. The regenerate non-streaming path already
guards with `if answer.strip():`; these lock in the same semantics everywhere.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main
from app.schemas import AskRequest, AskResponse

SSEEvent = dict[str, Any]


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def _install_stream(monkeypatch: pytest.MonkeyPatch, events: list[SSEEvent]) -> None:
    def fake_stream(
        req: AskRequest, routing_question: str | None = None
    ) -> Iterator[SSEEvent]:
        yield from events

    monkeypatch.setattr(app.main, "stream_orchestrator", fake_stream)


def _sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        name, data = "", "{}"
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        frames.append((name, json.loads(data)))
    return frames


def _roles(client: TestClient, cid: int) -> list[str]:
    return [m["role"] for m in client.get(f"/v1/conversations/{cid}/messages").json()]


# --- non-streaming ask -------------------------------------------------------


def test_nonstream_ask_empty_answer_writes_no_assistant_bubble(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app.main,
        "run_orchestrator",
        lambda req, routing_question=None: AskResponse(
            answer="", mode_used="auto->fast", notes="rate limited"
        ),
    )
    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert r.status_code == 200
    assert r.json()["answer"] == ""  # the failure is still reported to the client
    assert _roles(client, cid) == ["user"]  # ...but no empty assistant row


def test_nonstream_ask_real_answer_is_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not regress the happy path."""
    monkeypatch.setattr(
        app.main,
        "run_orchestrator",
        lambda req, routing_question=None: AskResponse(
            answer="real answer", mode_used="auto->fast", notes="n"
        ),
    )
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert _roles(client, cid) == ["user", "assistant"]


# --- streaming ask -----------------------------------------------------------


def test_stream_ask_empty_done_writes_no_assistant_bubble(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stream(
        monkeypatch,
        [
            {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}},
            {
                "event": "done",
                "data": {"answer": "", "mode_used": "auto->fast", "notes": "n"},
            },
        ],
    )
    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})

    assert r.status_code == 200
    assert _roles(client, cid) == ["user"]  # no empty assistant row
    done = next(d for n, d in _sse(r.text) if n == "done")
    assert "not saved (empty answer)" in done["notes"]  # client is told


# --- streaming regenerate (the data-loss bug) --------------------------------


def test_stream_regenerate_empty_done_preserves_old_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a good streamed answer.
    _install_stream(
        monkeypatch,
        [
            {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}},
            {"event": "delta", "data": {"text": "good answer"}},
            {
                "event": "done",
                "data": {
                    "answer": "good answer",
                    "mode_used": "auto->fast",
                    "notes": "n",
                },
            },
        ],
    )
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})

    # Regeneration completes but yields an EMPTY answer (e.g. reasoning truncation).
    _install_stream(
        monkeypatch,
        [
            {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}},
            {
                "event": "done",
                "data": {"answer": "", "mode_used": "auto->fast", "notes": "n"},
            },
        ],
    )
    res = client.post(f"/v1/conversations/{cid}/regenerate/stream", json={})
    assert res.status_code == 200

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    # The prior good answer survives — the empty done neither deleted nor blanked it.
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "good answer"
