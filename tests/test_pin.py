from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main
from app.schemas import AskRequest, AskResponse, ConversationPin, Mode


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    """Replace run_orchestrator with a canned response; record every request."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest, routing_question: str | None = None, owner: str | None = None
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _pin(client: TestClient, cid: int, model: str):
    return client.put(f"/v1/conversations/{cid}/pin", json={"model": model})


# --- schema validation -------------------------------------------------------


def test_conversation_pin_validation() -> None:
    assert ConversationPin(model="claude-sonnet-5").model == "claude-sonnet-5"
    assert ConversationPin(model="  smart ").model == "smart"
    assert ConversationPin(model="").model == ""
    assert ConversationPin().model == ""
    with pytest.raises(ValidationError):
        ConversationPin(model="bad model!!")


# --- pin endpoint + persistence ----------------------------------------------


def test_new_conversation_is_unpinned(client: TestClient) -> None:
    cid = _create(client)
    conv = client.get("/v1/conversations").json()[0]
    assert conv["id"] == cid
    assert conv["pinned_model"] is None


def test_pin_set_reflected_and_cleared(client: TestClient) -> None:
    cid = _create(client)

    res = _pin(client, cid, "claude-sonnet-5")
    assert res.status_code == 200
    assert res.json()["pinned_model"] == "claude-sonnet-5"

    listed = next(c for c in client.get("/v1/conversations").json() if c["id"] == cid)
    assert listed["pinned_model"] == "claude-sonnet-5"

    cleared = _pin(client, cid, "")
    assert cleared.status_code == 200
    assert cleared.json()["pinned_model"] is None


def test_pin_rejects_malformed_model(client: TestClient) -> None:
    cid = _create(client)
    assert _pin(client, cid, "bad model!!").status_code == 422


def test_pin_404_for_missing_conversation(client: TestClient) -> None:
    assert (
        client.put("/v1/conversations/999999/pin", json={"model": "gpt-5"}).status_code
        == 404
    )


def test_pin_requires_auth(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert _pin(client, cid, "gpt-5").status_code == 401
    ok = client.put(
        f"/v1/conversations/{cid}/pin",
        json={"model": "gpt-5"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 200


# --- routing honours the pin -------------------------------------------------


def test_ask_forces_the_pinned_model(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _pin(client, cid, "claude-sonnet-5")

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi", "mode": "auto"})

    sent = orchestrator_calls[-1]
    assert sent.model == "claude-sonnet-5"  # forced model (bypasses router + cache)


def test_model_pin_uses_smart_budget_regardless_of_request_mode(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    # The mode dropdown is disabled while pinned, so a stale "fast" mode must not
    # cramp a pinned heavy model to the fast-tier budget — it uses the smart tier.
    cid = _create(client)
    _pin(client, cid, "gpt-5")

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi", "mode": "fast"})

    sent = orchestrator_calls[-1]
    assert sent.model == "gpt-5"
    assert sent.mode == Mode.smart


def test_ask_uses_the_pinned_tier(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _pin(client, cid, "smart")

    # Even with the request asking for "fast", the pin decides.
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi", "mode": "fast"})

    sent = orchestrator_calls[-1]
    assert sent.mode == Mode.smart
    assert sent.model is None


def test_ask_without_pin_uses_request_mode(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi", "mode": "fast"})

    sent = orchestrator_calls[-1]
    assert sent.mode == Mode.fast
    assert sent.model is None


def test_streaming_ask_honours_pin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[AskRequest] = []

    def fake_stream(
        req: AskRequest, routing_question: str | None = None, owner: str | None = None
    ) -> Iterator[dict[str, Any]]:
        calls.append(req)
        yield {
            "event": "meta",
            "data": {"mode_used": "forced:x", "model": "x", "notes": "n"},
        }
        yield {
            "event": "done",
            "data": {"answer": "a", "mode_used": "forced:x", "notes": "n"},
        }

    monkeypatch.setattr(app.main, "stream_orchestrator", fake_stream)

    cid = _create(client)
    _pin(client, cid, "gpt-5-mini")
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "hi"})

    assert calls[-1].model == "gpt-5-mini"
