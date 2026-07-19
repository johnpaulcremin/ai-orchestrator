from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main
from app.orchestrator import _cache_key
from app.routing import decide_route
from app.schemas import AskRequest, AskResponse, Mode


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    """Replace run_orchestrator with a canned response; record every request."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(req: AskRequest) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=f"canned:{len(calls)}",
            mode_used=(f"forced:{req.model}" if req.model else "auto->fast"),
            notes="canned notes",
        )

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run_orchestrator)
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


# --- schema validation -------------------------------------------------------


def test_ask_request_validates_forced_model() -> None:
    assert (
        AskRequest(question="hi", model="  claude-sonnet-5 ").model == "claude-sonnet-5"
    )
    assert AskRequest(question="hi", model="").model is None
    assert AskRequest(question="hi").model is None
    with pytest.raises(ValidationError):
        AskRequest(question="hi", model="bad model!!")


# --- routing + cache with a forced model -------------------------------------


def test_decide_route_forced_model_bypasses_routing() -> None:
    decision = decide_route("anything", Mode.auto, forced_model="claude-sonnet-5")
    assert decision.model == "claude-sonnet-5"
    assert decision.mode_used == "forced:claude-sonnet-5"
    assert "Forced model" in decision.notes


def test_forced_model_skips_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    assert _cache_key(AskRequest(question="q", model="claude-sonnet-5")) is None
    # ...but an unforced request of the same prompt is cacheable.
    assert _cache_key(AskRequest(question="q")) is not None


# --- regenerate endpoint -----------------------------------------------------


def test_regenerate_replaces_last_assistant_message(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in before] == ["user", "assistant"]
    old_assistant_id = before[1]["id"]

    res = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    # Still exactly one assistant message — replaced, not appended.
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[1]["id"] != old_assistant_id
    assert after[1]["content"].startswith("canned")
    assert "regenerated" in after[1]["notes"]


def test_regenerate_with_no_user_message_is_400(client: TestClient) -> None:
    cid = _create(client, "empty")
    assert (
        client.post(f"/v1/conversations/{cid}/regenerate", json={}).status_code == 400
    )


def test_regenerate_forwards_forced_model_and_skips_cache(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    orchestrator_calls.clear()

    client.post(
        f"/v1/conversations/{cid}/regenerate",
        json={"model": "claude-sonnet-5", "mode": "smart"},
    )

    sent = orchestrator_calls[-1]
    assert sent.model == "claude-sonnet-5"
    assert sent.mode == Mode.smart
    assert sent.no_cache is True


def test_regenerate_reuses_last_question_with_prior_context(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "second question")
    orchestrator_calls.clear()

    client.post(f"/v1/conversations/{cid}/regenerate", json={})

    prompt = orchestrator_calls[-1].question
    assert "second question" in prompt  # the last user question is re-asked
    assert "first question" in prompt  # earlier turn is included as context


def test_regenerate_404_for_missing_conversation(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    assert (
        client.post("/v1/conversations/999999/regenerate", json={}).status_code == 404
    )
    assert orchestrator_calls == []


def test_failed_regeneration_preserves_the_old_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed a good answer.
    monkeypatch.setattr(
        app.main,
        "run_orchestrator",
        lambda req: AskResponse(
            answer="good answer", mode_used="auto->fast", notes="n"
        ),
    )
    cid = _create(client)
    _ask(client, cid, "hello")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    old_id = before[1]["id"]
    assert before[1]["content"] == "good answer"

    # Now the model fails (empty answer, e.g. rate-limited).
    monkeypatch.setattr(
        app.main,
        "run_orchestrator",
        lambda req: AskResponse(
            answer="", mode_used="auto->fast", notes="rate limited"
        ),
    )
    res = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert res.status_code == 200
    assert res.json()["answer"] == ""  # the failure is reported...

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    # ...but the previous good answer is untouched, not deleted or blanked.
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[1]["id"] == old_id
    assert after[1]["content"] == "good answer"


def test_regenerate_stream_error_preserves_old_answer(
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

    # The regeneration errors before producing any text.
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
    res = client.post(f"/v1/conversations/{cid}/regenerate/stream", json={})
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    # The old answer survives; no empty/partial message replaced it.
    assert [m["role"] for m in after] == ["user", "assistant"]
    assert after[1]["content"] == "old answer"


# --- streaming regenerate ----------------------------------------------------


def _install_stream(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]
) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_stream(req: AskRequest) -> Iterator[dict[str, Any]]:
        calls.append(req)
        yield from events

    monkeypatch.setattr(app.main, "stream_orchestrator", fake_stream)
    return calls


def test_regenerate_stream_replaces_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First seed a conversation with an ordinary streamed answer.
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

    # Now regenerate with a scripted fresh answer.
    regen = [
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
    calls = _install_stream(monkeypatch, regen)

    res = client.post(
        f"/v1/conversations/{cid}/regenerate/stream", json={"model": "gpt-5"}
    )
    assert res.status_code == 200

    # The regeneration re-asked the last user question, forced + no_cache.
    assert calls[-1].model == "gpt-5"
    assert calls[-1].no_cache is True
    assert "hi" in calls[-1].question

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "new answer"
    assert "regenerated" in messages[1]["notes"]


def test_regenerate_stream_404_for_missing_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_stream(monkeypatch, [])
    res = client.post("/v1/conversations/999999/regenerate/stream", json={})
    assert res.status_code == 404
    assert calls == []
