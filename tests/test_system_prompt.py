"""Per-conversation custom instructions (system prompt): set/clear via
PUT /v1/conversations/{id}/system_prompt, and threaded into every question
built for that conversation (ask, ask/stream, regenerate, edit).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main
from app.schemas import AskRequest, AskResponse, ConversationSystemPrompt


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
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=f"canned:{len(calls)}", mode_used="auto->fast", notes="n"
        )

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _set_instructions(client: TestClient, cid: int, text: str):
    return client.put(
        f"/v1/conversations/{cid}/system_prompt", json={"system_prompt": text}
    )


def _ask(client: TestClient, cid: int, question: str):
    return client.post(f"/v1/conversations/{cid}/ask", json={"question": question})


# --- schema validation -------------------------------------------------------


def test_system_prompt_schema_strips_and_bounds() -> None:
    assert (
        ConversationSystemPrompt(system_prompt="  be terse  ").system_prompt
        == "  be terse  "
    )
    assert ConversationSystemPrompt().system_prompt == ""
    with pytest.raises(ValidationError):
        ConversationSystemPrompt(system_prompt="x" * 4001)


# --- endpoint + persistence ---------------------------------------------------


def test_new_conversation_has_no_instructions(client: TestClient) -> None:
    cid = _create(client)
    conv = next(c for c in client.get("/v1/conversations").json() if c["id"] == cid)
    assert conv["system_prompt"] is None


def test_set_instructions_reflected_and_cleared(client: TestClient) -> None:
    cid = _create(client)

    res = _set_instructions(client, cid, "Always answer in French.")
    assert res.status_code == 200
    assert res.json()["system_prompt"] == "Always answer in French."

    listed = next(c for c in client.get("/v1/conversations").json() if c["id"] == cid)
    assert listed["system_prompt"] == "Always answer in French."

    cleared = _set_instructions(client, cid, "")
    assert cleared.status_code == 200
    assert cleared.json()["system_prompt"] is None


def test_set_instructions_404_for_missing_conversation(client: TestClient) -> None:
    res = _set_instructions(client, 999999, "hi")
    assert res.status_code == 404


def test_set_instructions_over_max_length_is_422(client: TestClient) -> None:
    cid = _create(client)
    res = _set_instructions(client, cid, "x" * 4001)
    assert res.status_code == 422


# --- applied to the assembled prompt ------------------------------------------


def test_instructions_apply_on_the_first_message(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _set_instructions(client, cid, "Always answer in French.")

    _ask(client, cid, "What is the capital of Spain?")

    prompt = orchestrator_calls[-1].question
    assert "Instructions for this conversation:" in prompt
    assert "Always answer in French." in prompt
    assert "What is the capital of Spain?" in prompt
    # No history yet: the history-framing block must not appear.
    assert "Conversation history:" not in prompt


def test_instructions_apply_alongside_history(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _set_instructions(client, cid, "Always answer in French.")

    _ask(client, cid, "first question")
    _ask(client, cid, "second question")

    prompt = orchestrator_calls[-1].question
    assert "Instructions for this conversation:" in prompt
    assert "Always answer in French." in prompt
    assert "Conversation history:" in prompt
    assert "first question" in prompt


def test_no_instructions_leaves_first_message_unwrapped(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "plain question")

    assert orchestrator_calls[-1].question == "plain question"


def test_instructions_apply_to_regenerate(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "first question")
    _set_instructions(client, cid, "Be extremely terse.")
    orchestrator_calls.clear()

    res = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert res.status_code == 200

    prompt = orchestrator_calls[-1].question
    assert "Instructions for this conversation:" in prompt
    assert "Be extremely terse." in prompt


def test_instructions_apply_to_edit(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    _set_instructions(client, cid, "Respond only in haiku.")
    orchestrator_calls.clear()

    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    res = client.post(
        f"/v1/conversations/{cid}/messages/{user_id}/edit",
        json={"question": "hello, edited"},
    )
    assert res.status_code == 200

    prompt = orchestrator_calls[-1].question
    assert "Instructions for this conversation:" in prompt
    assert "Respond only in haiku." in prompt


# --- ownership -----------------------------------------------------------------


def test_instructions_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "instructions-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )
    bob = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "password123"}
    ).json()["access_token"]

    cid = client.post(
        "/v1/conversations",
        json={"title": "alice's chat"},
        headers={"Authorization": f"Bearer {alice}"},
    ).json()["id"]

    res = client.put(
        f"/v1/conversations/{cid}/system_prompt",
        json={"system_prompt": "hijack"},
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.status_code == 404
