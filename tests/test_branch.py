"""Branching a conversation from a message
(POST /v1/conversations/{id}/messages/{message_id}/branch).

Like duplicate, but truncated: only messages up to and including the given
message are copied — for exploring an alternate reply to an earlier point
without disturbing the original conversation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app.schemas import AskRequest, AskResponse


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


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
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def test_branch_copies_only_messages_up_to_the_given_one(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Trip to Japan")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "any good ramen?"})
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "what about sushi?"})
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert len(messages) == 4
    branch_point = messages[1]["id"]  # first assistant reply

    res = client.post(f"/v1/conversations/{cid}/messages/{branch_point}/branch")
    assert res.status_code == 200
    branch = res.json()
    assert branch["id"] != cid
    assert branch["title"] == "Trip to Japan (branch)"

    branch_messages = client.get(f"/v1/conversations/{branch['id']}/messages").json()
    assert [m["content"] for m in branch_messages] == [
        "any good ramen?",
        "canned answer",
    ]


def test_branch_copies_pin_and_instructions(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    client.put(f"/v1/conversations/{cid}/pin", json={"model": "claude-sonnet-5"})
    client.put(
        f"/v1/conversations/{cid}/system_prompt", json={"system_prompt": "Be terse."}
    )
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]

    res = client.post(f"/v1/conversations/{cid}/messages/{message_id}/branch")
    assert res.status_code == 200
    branch = res.json()
    assert branch["pinned_model"] == "claude-sonnet-5"
    assert branch["system_prompt"] == "Be terse."


def test_branch_fresh_ids_not_the_originals(
    client: TestClient, orchestrator_calls: list[AskRequest], db_path: Path
) -> None:
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    original_messages = client.get(f"/v1/conversations/{cid}/messages").json()
    message_id = original_messages[-1]["id"]

    res = client.post(f"/v1/conversations/{cid}/messages/{message_id}/branch")
    branch_id = res.json()["id"]

    branch_messages = client.get(f"/v1/conversations/{branch_id}/messages").json()
    original_ids = {m["id"] for m in original_messages}
    assert not original_ids & {m["id"] for m in branch_messages}


def test_branch_nonexistent_conversation_is_404(client: TestClient) -> None:
    res = client.post("/v1/conversations/999999/messages/1/branch")
    assert res.status_code == 404


def test_branch_nonexistent_message_is_404(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    res = client.post(f"/v1/conversations/{cid}/messages/999999/branch")
    assert res.status_code == 404


def test_branch_of_a_message_from_a_different_conversation_is_404(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid_a = _create(client, "a")
    cid_b = _create(client, "b")
    client.post(f"/v1/conversations/{cid_b}/ask", json={"question": "hi"})
    other_message_id = client.get(f"/v1/conversations/{cid_b}/messages").json()[0]["id"]

    res = client.post(f"/v1/conversations/{cid_a}/messages/{other_message_id}/branch")
    assert res.status_code == 404


def test_branch_is_owned_by_the_brancher(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "branch-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
    ) -> AskResponse:
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)

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
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    message_id = client.get(
        f"/v1/conversations/{cid}/messages",
        headers={"Authorization": f"Bearer {alice}"},
    ).json()[0]["id"]

    # Bob cannot branch a conversation he doesn't own.
    assert (
        client.post(
            f"/v1/conversations/{cid}/messages/{message_id}/branch",
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )

    res = client.post(
        f"/v1/conversations/{cid}/messages/{message_id}/branch",
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert res.status_code == 200
    assert res.json()["owner"] == "alice"
