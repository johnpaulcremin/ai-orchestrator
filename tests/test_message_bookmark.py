"""Bookmarking a single message (PUT /v1/conversations/{id}/messages/{message_id}/bookmark).

A marker on one turn, distinct from favoriting the whole conversation
(`PUT /v1/conversations/{id}/favorite`).
"""

from __future__ import annotations

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
        library_sources: list[dict] | None = None,
        memory_sources: list[dict] | None = None,
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

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


def test_new_messages_default_to_not_bookmarked(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert all(m["bookmarked"] is False for m in messages)


def test_bookmarks_a_message(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )
    assert res.status_code == 200
    assert res.json()["bookmarked"] is True

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    bookmarked = [m for m in messages if m["id"] == message_id][0]
    assert bookmarked["bookmarked"] is True


def test_unbookmarks_a_message(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )
    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": False},
    )
    assert res.status_code == 200
    assert res.json()["bookmarked"] is False


def test_bookmarking_one_message_does_not_affect_others(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id, assistant_id = messages[0]["id"], messages[1]["id"]

    client.put(
        f"/v1/conversations/{cid}/messages/{user_id}/bookmark",
        json={"bookmarked": True},
    )

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    by_id = {m["id"]: m for m in after}
    assert by_id[user_id]["bookmarked"] is True
    assert by_id[assistant_id]["bookmarked"] is False


def test_bookmarking_does_not_touch_conversation_updated_at(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]
    before_updated_at = client.get("/v1/conversations").json()[0]["updated_at"]

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )

    after_updated_at = client.get("/v1/conversations").json()[0]["updated_at"]
    assert after_updated_at == before_updated_at


def test_bookmark_nonexistent_message_is_404(client: TestClient) -> None:
    cid = _create(client)
    res = client.put(
        f"/v1/conversations/{cid}/messages/999999/bookmark",
        json={"bookmarked": True},
    )
    assert res.status_code == 404


def test_bookmark_404_for_missing_conversation(client: TestClient) -> None:
    res = client.put(
        "/v1/conversations/999999/messages/1/bookmark",
        json={"bookmarked": True},
    )
    assert res.status_code == 404


def test_bookmark_scoped_to_its_conversation(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid_a = _create(client, "a")
    cid_b = _create(client, "b")
    _ask(client, cid_a, "hello")
    message_id = client.get(f"/v1/conversations/{cid_a}/messages").json()[0]["id"]

    # Attempting to bookmark conversation A's message through conversation
    # B's URL must not succeed — otherwise a client could act across
    # conversation boundaries.
    res = client.put(
        f"/v1/conversations/{cid_b}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )
    assert res.status_code == 404

    still_unbookmarked = client.get(f"/v1/conversations/{cid_a}/messages").json()
    assert still_unbookmarked[0]["bookmarked"] is False


def test_bookmark_scoped_to_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_calls: list[AskRequest],
) -> None:
    monkeypatch.setenv("JWT_SECRET", "bookmark-secret")
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
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    message_id = client.get(
        f"/v1/conversations/{cid}/messages",
        headers={"Authorization": f"Bearer {alice}"},
    ).json()[0]["id"]

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.status_code == 404
