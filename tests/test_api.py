from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main
from app.schemas import AskRequest, AskResponse


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    """Replace run_orchestrator with a canned response; record every request."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest, routing_question: str | None = None, owner: str | None = None
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer="canned answer",
            mode_used="auto->fast",
            notes="canned notes",
        )

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create_conversation(client: TestClient, title: str | None = None) -> int:
    payload = {} if title is None else {"title": title}
    response = client.post("/v1/conversations", json=payload)
    assert response.status_code == 200
    return int(response.json()["id"])


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ai-orchestrator"}


def test_status_is_enriched(client: TestClient) -> None:
    response = client.get("/v1/status")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "ai-orchestrator"
    assert body["auth_enabled"] is False
    assert set(body["models"]) == {"router", "fast", "smart", "fallback"}


def test_conversation_crud(client: TestClient) -> None:
    # Create with an explicit title and with the default title.
    custom_id = _create_conversation(client, "My custom title")
    default_response = client.post("/v1/conversations", json={})
    assert default_response.status_code == 200
    assert default_response.json()["title"] == "Untitled conversation"

    # List contains both.
    listed = client.get("/v1/conversations")
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()}
    assert {custom_id, default_response.json()["id"]} <= ids

    # Rename.
    renamed = client.patch(
        f"/v1/conversations/{custom_id}",
        json={"title": "Renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed"

    # Messages listing starts empty.
    messages = client.get(f"/v1/conversations/{custom_id}/messages")
    assert messages.status_code == 200
    assert messages.json() == []

    # Delete, then the conversation is gone.
    deleted = client.delete(f"/v1/conversations/{custom_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert client.delete(f"/v1/conversations/{custom_id}").status_code == 404


def test_conversation_404s_for_missing_id(client: TestClient) -> None:
    assert (
        client.patch("/v1/conversations/999999", json={"title": "x"}).status_code == 404
    )
    assert client.delete("/v1/conversations/999999").status_code == 404
    assert client.get("/v1/conversations/999999/messages").status_code == 404


def test_ask_404_for_missing_conversation(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    response = client.post(
        "/v1/conversations/999999/ask",
        json={"question": "Hello?"},
    )
    assert response.status_code == 404
    assert orchestrator_calls == []


def test_ask_persists_user_and_assistant_messages(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    conversation_id = _create_conversation(client, "My custom title")
    question = "What is 2+2?"

    response = client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": question, "mode": "auto"},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["answer"] == "canned answer"
    assert body["mode_used"] == "auto->fast"
    assert body["notes"] == "canned notes | context_messages=0"

    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert len(messages) == 2

    user_message, assistant_message = messages
    assert user_message["role"] == "user"
    assert user_message["content"] == question

    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "canned answer"
    assert assistant_message["mode_used"] == "auto->fast"
    assert assistant_message["notes"] == "canned notes | context_messages=0"


def test_auto_title_set_from_first_question(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    conversation_id = _create_conversation(client)  # "Untitled conversation"
    question = "What is the capital of France?"

    client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": question},
    )

    listed = client.get("/v1/conversations").json()
    titles = {row["id"]: row["title"] for row in listed}
    assert titles[conversation_id] == question


def test_auto_title_truncates_long_question(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    conversation_id = _create_conversation(client)
    question = "word " * 30  # normalises to 149 characters
    clean_question = " ".join(question.split())
    expected_title = f"{clean_question[:70].rstrip()}..."

    client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": question},
    )

    listed = client.get("/v1/conversations").json()
    titles = {row["id"]: row["title"] for row in listed}
    assert titles[conversation_id] == expected_title
    assert len(titles[conversation_id]) <= 73


def test_custom_title_is_not_overwritten(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    conversation_id = _create_conversation(client, "My custom title")

    client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": "Some question"},
    )

    listed = client.get("/v1/conversations").json()
    titles = {row["id"]: row["title"] for row in listed}
    assert titles[conversation_id] == "My custom title"


def test_second_ask_passes_context_to_orchestrator(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    conversation_id = _create_conversation(client, "Context test")
    first_question = "Remember the number 42."
    second_question = "What number did I mention?"

    client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": first_question},
    )
    client.post(
        f"/v1/conversations/{conversation_id}/ask",
        json={"question": second_question},
    )

    assert len(orchestrator_calls) == 2

    # First ask has no history, so the question passes through untouched.
    assert orchestrator_calls[0].question == first_question

    # Second ask carries the prior exchange plus the current question.
    contextual = orchestrator_calls[1].question
    assert f"USER: {first_question}" in contextual
    assert "ASSISTANT: canned answer" in contextual
    assert "Current user question:" in contextual
    assert second_question in contextual

    # And the response notes count the two prior messages.
    messages = client.get(f"/v1/conversations/{conversation_id}/messages").json()
    assert messages[-1]["notes"] == "canned notes | context_messages=2"
