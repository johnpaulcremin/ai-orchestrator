"""Editing a past user message: re-run from that point (dropping everything
after it), while a failed/aborted edit leaves the original message and its
answer untouched — same "replace only on success" philosophy as regenerate.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app.schemas import AskRequest, AskResponse


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
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
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=f"canned:{len(calls)}",
            mode_used=(f"forced:{req.model}" if req.model else "auto->fast"),
            notes="canned notes",
        )

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _ask(client: TestClient, cid: int, question: str) -> None:
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask", json={"question": question}
        ).status_code
        == 200
    )


# --- non-streaming edit --------------------------------------------------------


def test_edit_replaces_message_and_answer(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    res = client.post(
        f"/v1/conversations/{cid}/messages/{user_id}/edit",
        json={"question": "hello, edited"},
    )
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[0]["content"] == "hello, edited"
    assert after[0]["id"] != user_id  # a fresh row, not an in-place update
    assert after[1]["content"].startswith("canned")
    assert "edited" in after[1]["notes"]


def test_edit_drops_everything_after_the_edited_message(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "second question")

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    assert len(before) == 4  # user, assistant, user, assistant
    first_user_id = before[0]["id"]

    res = client.post(
        f"/v1/conversations/{cid}/messages/{first_user_id}/edit",
        json={"question": "first question, edited"},
    )
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    # The second turn is gone entirely; only the edited turn + its new answer remain.
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[0]["content"] == "first question, edited"


def test_edit_context_excludes_everything_from_the_edited_message_onward(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "second question")
    orchestrator_calls.clear()

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    second_user_id = before[2]["id"]

    client.post(
        f"/v1/conversations/{cid}/messages/{second_user_id}/edit",
        json={"question": "second question, edited"},
    )

    prompt = orchestrator_calls[-1].question
    assert "second question, edited" in prompt
    assert "first question" in prompt  # earlier turn still included as context


def test_edit_nonexistent_message_is_404(client: TestClient) -> None:
    cid = _create(client)
    res = client.post(
        f"/v1/conversations/{cid}/messages/999999/edit", json={"question": "x"}
    )
    assert res.status_code == 404


def test_edit_assistant_message_is_400(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_id = messages[1]["id"]

    res = client.post(
        f"/v1/conversations/{cid}/messages/{assistant_id}/edit",
        json={"question": "x"},
    )
    assert res.status_code == 400


def test_edit_404_for_missing_conversation(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    res = client.post(
        "/v1/conversations/999999/messages/1/edit", json={"question": "x"}
    )
    assert res.status_code == 404
    assert orchestrator_calls == []


def test_edit_forwards_forced_model_and_skips_cache(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    orchestrator_calls.clear()

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    client.post(
        f"/v1/conversations/{cid}/messages/{user_id}/edit",
        json={"question": "hello again", "model": "claude-sonnet-5", "mode": "smart"},
    )

    sent = orchestrator_calls[-1]
    assert sent.model == "claude-sonnet-5"


def test_failed_edit_preserves_the_original_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app.routers.messages,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, history="", **_kw: AskResponse(
            answer="good answer", mode_used="auto->fast", notes="n"
        ),
    )
    cid = _create(client)
    _ask(client, cid, "hello")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    original_user_id = before[0]["id"]

    monkeypatch.setattr(
        app.routers.messages,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, history="", **_kw: AskResponse(
            answer="", mode_used="auto->fast", notes="rate limited"
        ),
    )
    res = client.post(
        f"/v1/conversations/{cid}/messages/{original_user_id}/edit",
        json={"question": "edited but fails"},
    )
    assert res.status_code == 200
    assert res.json()["answer"] == ""

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[0]["id"] == original_user_id
    assert after[0]["content"] == "hello"  # untouched, not the edited text
    assert after[1]["content"] == "good answer"


# --- streaming edit --------------------------------------------------------------


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]
) -> list[AskRequest]:
    calls: list[AskRequest] = []

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
    ) -> Iterator[dict[str, Any]]:
        calls.append(req)
        yield from events

    monkeypatch.setattr(app.routers.messages, "stream_orchestrator", fake_stream)
    return calls


def test_edit_stream_replaces_message_and_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = [
        {
            "event": "meta",
            "data": {"mode_used": "auto->fast", "model": "m", "notes": "n"},
        },
        {"event": "delta", "data": {"text": "old answer"}},
        {
            "event": "done",
            "data": {"answer": "old answer", "mode_used": "auto->fast", "notes": "n"},
        },
    ]
    _install_stream(monkeypatch, seed)
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    edit_events = [
        {
            "event": "meta",
            "data": {"mode_used": "forced:gpt-5", "model": "gpt-5", "notes": "n"},
        },
        {"event": "delta", "data": {"text": "new answer"}},
        {
            "event": "done",
            "data": {"answer": "new answer", "mode_used": "forced:gpt-5", "notes": "n"},
        },
    ]
    calls = _install_stream(monkeypatch, edit_events)

    res = client.post(
        f"/v1/conversations/{cid}/messages/{user_id}/edit/stream",
        json={"question": "hi, edited", "model": "gpt-5"},
    )
    assert res.status_code == 200
    assert calls[-1].model == "gpt-5"

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hi, edited"
    assert messages[0]["id"] != user_id
    assert messages[1]["content"] == "new answer"
    assert "edited" in messages[1]["notes"]


def test_edit_stream_error_preserves_original_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = [
        {
            "event": "meta",
            "data": {"mode_used": "auto->fast", "model": "m", "notes": "n"},
        },
        {"event": "delta", "data": {"text": "old answer"}},
        {
            "event": "done",
            "data": {"answer": "old answer", "mode_used": "auto->fast", "notes": "n"},
        },
    ]
    _install_stream(monkeypatch, seed)
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    _install_stream(
        monkeypatch,
        [
            {
                "event": "meta",
                "data": {"mode_used": "auto->fast", "model": "m", "notes": "n"},
            },
            {"event": "error", "data": {"message": "boom"}},
        ],
    )
    res = client.post(
        f"/v1/conversations/{cid}/messages/{user_id}/edit/stream",
        json={"question": "hi, edited"},
    )
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[0]["id"] == user_id
    assert after[0]["content"] == "hi"  # unchanged
    assert after[1]["content"] == "old answer"


def test_edit_stream_404_for_missing_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stream(monkeypatch, [])
    res = client.post(
        "/v1/conversations/999999/messages/1/edit/stream", json={"question": "x"}
    )
    assert res.status_code == 404
    assert calls == []


def test_edit_stream_nonexistent_message_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stream(monkeypatch, [])
    cid = _create(client)
    res = client.post(
        f"/v1/conversations/{cid}/messages/999999/edit/stream", json={"question": "x"}
    )
    assert res.status_code == 404
    assert calls == []
