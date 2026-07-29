"""Listing bookmarked messages across every conversation (GET /v1/bookmarks)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app.schemas import AskRequest, AskResponse

JWT_SECRET = "bookmarks-secret"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


def _register_login(
    client: TestClient, username: str, password: str = "password123"
) -> str:
    client.post("/v1/auth/register", json={"username": username, "password": password})
    resp = client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    return str(resp.json()["access_token"])


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(
    client: TestClient, title: str, headers: dict[str, str] | None = None
) -> int:
    return int(
        client.post("/v1/conversations", json={"title": title}, headers=headers).json()[
            "id"
        ]
    )


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
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def test_no_bookmarks_returns_empty_list(client: TestClient) -> None:
    _create(client, "Untitled conversation")
    res = client.get("/v1/bookmarks")
    assert res.status_code == 200
    assert res.json() == []


def test_lists_a_bookmarked_message_with_its_conversation_title(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Trip planning")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "where to go"})
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )

    res = client.get("/v1/bookmarks")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["id"] == message_id
    assert body[0]["conversation_id"] == cid
    assert body[0]["conversation_title"] == "Trip planning"
    assert body[0]["bookmarked"] is True


def test_excludes_unbookmarked_messages(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Untitled conversation")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hello"})

    res = client.get("/v1/bookmarks")
    assert res.json() == []


def test_unbookmarking_removes_it_from_the_list(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Untitled conversation")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hello"})
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": True},
    )
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/bookmark",
        json={"bookmarked": False},
    )

    res = client.get("/v1/bookmarks")
    assert res.json() == []


def test_lists_bookmarks_across_multiple_conversations_newest_first(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    first_cid = _create(client, "First conversation")
    client.post(f"/v1/conversations/{first_cid}/ask", json={"question": "one"})
    first_message_id = client.get(f"/v1/conversations/{first_cid}/messages").json()[0][
        "id"
    ]
    client.put(
        f"/v1/conversations/{first_cid}/messages/{first_message_id}/bookmark",
        json={"bookmarked": True},
    )

    second_cid = _create(client, "Second conversation")
    client.post(f"/v1/conversations/{second_cid}/ask", json={"question": "two"})
    second_message_id = client.get(f"/v1/conversations/{second_cid}/messages").json()[
        0
    ]["id"]
    client.put(
        f"/v1/conversations/{second_cid}/messages/{second_message_id}/bookmark",
        json={"bookmarked": True},
    )

    res = client.get("/v1/bookmarks")
    ids = [item["id"] for item in res.json()]
    assert ids == [second_message_id, first_message_id]


def test_bookmarks_are_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
    ) -> AskResponse:
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)

    alice_cid = _create(client, "Alice's conversation", headers=_hdr(alice))
    client.post(
        f"/v1/conversations/{alice_cid}/ask",
        json={"question": "hi"},
        headers=_hdr(alice),
    )
    alice_message_id = client.get(
        f"/v1/conversations/{alice_cid}/messages", headers=_hdr(alice)
    ).json()[0]["id"]
    client.put(
        f"/v1/conversations/{alice_cid}/messages/{alice_message_id}/bookmark",
        json={"bookmarked": True},
        headers=_hdr(alice),
    )

    bob_results = client.get("/v1/bookmarks", headers=_hdr(bob)).json()
    alice_results = client.get("/v1/bookmarks", headers=_hdr(alice)).json()

    assert bob_results == []
    assert [item["id"] for item in alice_results] == [alice_message_id]
