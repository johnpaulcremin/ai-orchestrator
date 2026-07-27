"""Deleting a single message (DELETE /v1/conversations/{id}/messages/{message_id}).

Distinct from regenerate (replaces the last answer) and edit (discards a
whole range and re-asks): this removes exactly one message row and nothing
else.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main
from app.database import get_summary_cache, set_summary_cache
from app.schemas import AskRequest, AskResponse


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

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


def test_delete_user_message_leaves_the_answer(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    user_id = before[0]["id"]

    res = client.delete(f"/v1/conversations/{cid}/messages/{user_id}")
    assert res.status_code == 200
    assert res.json() == {"status": "deleted", "message_id": user_id}

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["assistant"]


def test_delete_assistant_message_leaves_the_question(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_id = before[1]["id"]

    res = client.delete(f"/v1/conversations/{cid}/messages/{assistant_id}")
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in after] == ["user"]


def test_delete_middle_message_leaves_other_turns_intact(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "second question")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    assert len(before) == 4
    first_assistant_id = before[1]["id"]

    res = client.delete(f"/v1/conversations/{cid}/messages/{first_assistant_id}")
    assert res.status_code == 200

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["content"] for m in after] == [
        "first question",
        "second question",
        "canned answer",
    ]


def test_delete_nonexistent_message_is_404(client: TestClient) -> None:
    cid = _create(client)
    res = client.delete(f"/v1/conversations/{cid}/messages/999999")
    assert res.status_code == 404


def test_delete_404_for_missing_conversation(client: TestClient) -> None:
    res = client.delete("/v1/conversations/999999/messages/1")
    assert res.status_code == 404


def test_delete_message_scoped_to_its_conversation(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid_a = _create(client, "a")
    cid_b = _create(client, "b")
    _ask(client, cid_a, "hello")
    message_id = client.get(f"/v1/conversations/{cid_a}/messages").json()[0]["id"]

    # Attempting to delete conversation A's message through conversation B's
    # URL must not succeed — otherwise a client could delete across
    # conversation boundaries.
    res = client.delete(f"/v1/conversations/{cid_b}/messages/{message_id}")
    assert res.status_code == 404

    still_there = client.get(f"/v1/conversations/{cid_a}/messages").json()
    assert len(still_there) == 2


def test_delete_message_invalidates_the_cached_history_summary(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    set_summary_cache(cid, 5, "a stale summary")
    assert get_summary_cache(cid) is not None

    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[0]["id"]
    res = client.delete(f"/v1/conversations/{cid}/messages/{message_id}")
    assert res.status_code == 200

    # An arbitrary message could have been in the already-summarized older
    # window, so the cache can no longer be trusted and must be dropped.
    assert get_summary_cache(cid) is None


def test_restore_message_recreates_it_with_a_new_id(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    _ask(client, cid, "hello")
    before = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = before[1]
    client.delete(f"/v1/conversations/{cid}/messages/{assistant['id']}")

    res = client.post(
        f"/v1/conversations/{cid}/messages/restore",
        json={
            "role": "assistant",
            "content": assistant["content"],
            "mode_used": assistant["mode_used"],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["id"] != assistant["id"]
    assert body["content"] == assistant["content"]
    assert body["role"] == "assistant"

    after = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["content"] for m in after] == ["hello", "canned answer"]


def test_restore_message_preserves_truncated_and_code_results(
    client: TestClient,
) -> None:
    cid = _create(client)
    res = client.post(
        f"/v1/conversations/{cid}/messages/restore",
        json={
            "role": "assistant",
            "content": "here's the code",
            "truncated": True,
            "code_results": [{"code": "print(1)", "logs": "1", "images": None}],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["truncated"] is True
    assert body["code_results"] == [{"code": "print(1)", "logs": "1", "images": None}]


def test_restore_message_404_for_missing_conversation(client: TestClient) -> None:
    res = client.post(
        "/v1/conversations/999999/messages/restore",
        json={"role": "user", "content": "hi"},
    )
    assert res.status_code == 404


def test_restore_message_422_for_empty_content(client: TestClient) -> None:
    cid = _create(client)
    res = client.post(
        f"/v1/conversations/{cid}/messages/restore",
        json={"role": "user", "content": ""},
    )
    assert res.status_code == 422


def test_restore_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "restore-secret")
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

    res = client.post(
        f"/v1/conversations/{cid}/messages/restore",
        json={"role": "user", "content": "hi"},
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.status_code == 404


def test_delete_scoped_to_owner(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator_calls: list[AskRequest],
) -> None:
    monkeypatch.setenv("JWT_SECRET", "delete-secret")
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

    res = client.delete(
        f"/v1/conversations/{cid}/messages/{message_id}",
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.status_code == 404
