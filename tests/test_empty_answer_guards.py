"""Empty/failed model answers must never destroy or pollute conversation history.

Three reachable cases with one root cause (an empty answer, persisted without a
guard): the non-streaming ask writing an empty assistant bubble, the streaming
ask doing the same, and — the worst — a streaming regenerate whose empty `done`
deletes the previous good answer. The regenerate non-streaming path already
guards with `if answer.strip():`; these lock in the same semantics everywhere.
"""

from __future__ import annotations

import json
import types
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
import app.orchestrator
import app.orchestrator_calls
from app.database import add_message
from app.schemas import AskRequest, AskResponse

SSEEvent = dict[str, Any]


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def _install_stream(monkeypatch: pytest.MonkeyPatch, events: list[SSEEvent]) -> None:
    def fake_stream(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        recall_library: bool = False,
        memory_sources: list[dict] | None = None,
    ) -> Iterator[SSEEvent]:
        yield from events

    monkeypatch.setattr(app.routers.messages, "stream_orchestrator", fake_stream)


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
        app.routers.messages,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, history="", **_kw: AskResponse(
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
        app.routers.messages,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, history="", **_kw: AskResponse(
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


def test_stream_regenerate_empty_done_still_records_what_it_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserving history is not the same as accounting for the attempt. This
    branch keeps the old answer AND pays for a full model call, and that cost
    used to reach only spend_log — which has no conversation_id, so nothing
    could tie it back to the turn that spent it (see
    app/retry_attribution.py's record_failed_attempt). Streaming twin of the
    two non-streaming guards covered in tests/test_retry_cost.py."""
    from app import retry_attribution
    from app.database import retry_log_turn_rows

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
                    "cost_usd": 0.01,
                },
            },
        ],
    )
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})

    _install_stream(
        monkeypatch,
        [
            {"event": "meta", "data": {"mode_used": "auto->smart", "model": "m2"}},
            {
                "event": "done",
                "data": {
                    "answer": "",
                    "mode_used": "auto->smart",
                    "notes": "n",
                    "cost_usd": 0.06,
                },
            },
        ],
    )
    assert (
        client.post(f"/v1/conversations/{cid}/regenerate/stream", json={}).status_code
        == 200
    )

    attempts = retry_log_turn_rows(None, days=1)
    assert [a["attempt_index"] for a in attempts] == [1, 2]
    # Attempt 1 is the original, still in place; attempt 2 is the failure, with
    # its own cost and no message id.
    assert attempts[0]["signal"] is None
    assert attempts[0]["cost_usd"] == pytest.approx(0.01)
    assert attempts[1]["signal"] == retry_attribution.SIGNAL_FAILED
    assert attempts[1]["cost_usd"] == pytest.approx(0.06)
    assert attempts[1]["message_id"] is None


def test_stream_ask_empty_done_records_no_failed_attempt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary ask that returns nothing is NOT a failed retry: there is no
    turn to attribute it to and nothing it replaced. The anchor is absent, so
    the attempt chain stays empty rather than inventing a turn."""
    from app.database import retry_log_turn_rows

    _install_stream(
        monkeypatch,
        [
            {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}},
            {
                "event": "done",
                "data": {
                    "answer": "",
                    "mode_used": "auto->fast",
                    "notes": "n",
                    "cost_usd": 0.02,
                },
            },
        ],
    )
    cid = _create(client)
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"}
        ).status_code
        == 200
    )

    assert retry_log_turn_rows(None, days=1) == []


# --- model-returned-empty on the non-stream path (no exception) --------------


def test_nonstream_regenerate_empty_output_preserves_old_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model can return an empty-output Response WITHOUT raising (reasoning
    truncated). _extract_text must yield '' so the regenerate guard fires — the
    repr must never replace the good prior answer.
    """

    def create(**_kwargs: Any) -> object:
        return types.SimpleNamespace(output_text="", usage=None)

    fake = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    fake.with_options = lambda **_kw: fake
    monkeypatch.setattr(app.orchestrator, "get_client", lambda: fake)
    # The outer guard call inside run_orchestrator resolves get_client via
    # app.orchestrator's own globals (re-exported), but the real _call_openai
    # dispatch (unpatched here) resolves its own get_client() call via
    # orchestrator_calls' globals — both must return the same fake client.
    monkeypatch.setattr(app.orchestrator_calls, "get_client", lambda: fake)

    cid = _create(client)
    add_message(conversation_id=cid, role="user", content="q")
    add_message(conversation_id=cid, role="assistant", content="good answer")

    # fast mode avoids a classifier call; the answer call returns empty output.
    res = client.post(f"/v1/conversations/{cid}/regenerate", json={"mode": "fast"})

    assert res.status_code == 200
    assert res.json()["answer"] == ""  # '' — not a 'namespace(...)' repr
    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[1]["content"] == "good answer"  # old answer intact, not a repr
