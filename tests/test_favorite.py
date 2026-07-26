"""Favorite conversations: the star toggle, sidebar sort order, and
propagation through Duplicate/Import (matching the pin/instructions field
parity established for those two)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _favorite(client: TestClient, cid: int, favorite: bool):
    return client.put(f"/v1/conversations/{cid}/favorite", json={"favorite": favorite})


# --- favorite endpoint + persistence ------------------------------------------


def test_new_conversation_is_not_favorited(client: TestClient) -> None:
    cid = _create(client)
    conv = client.get("/v1/conversations").json()[0]
    assert conv["id"] == cid
    assert conv["favorite"] is False


def test_favorite_set_and_cleared(client: TestClient) -> None:
    cid = _create(client)

    res = _favorite(client, cid, True)
    assert res.status_code == 200
    assert res.json()["favorite"] is True

    conv = client.get("/v1/conversations").json()[0]
    assert conv["favorite"] is True

    res = _favorite(client, cid, False)
    assert res.status_code == 200
    assert res.json()["favorite"] is False


def test_favorite_404_for_missing_conversation(client: TestClient) -> None:
    res = _favorite(client, 999999, True)
    assert res.status_code == 404


def test_favorite_requires_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert _favorite(client, cid, True).status_code == 401
    ok = client.put(
        f"/v1/conversations/{cid}/favorite",
        json={"favorite": True},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 200


def test_favoriting_does_not_bump_updated_at(
    client: TestClient, db_path: object
) -> None:
    import sqlite3

    cid = _create(client)
    before = client.get("/v1/conversations").json()[0]["updated_at"]

    # Backdate created_at/updated_at so a real clock tick can't mask the bug.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE conversations SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
        (cid,),
    )
    conn.commit()
    conn.close()
    backdated = client.get("/v1/conversations").json()[0]["updated_at"]
    assert backdated != before

    _favorite(client, cid, True)
    after = client.get("/v1/conversations").json()[0]["updated_at"]
    assert after == backdated


# --- sidebar sort order --------------------------------------------------------


def test_favorited_conversations_sort_first(client: TestClient) -> None:
    first = _create(client, "first")
    second = _create(client, "second")
    third = _create(client, "third")

    # Star the OLDEST one — it must still jump to the top, ahead of newer,
    # unfavorited conversations.
    _favorite(client, first, True)

    ids = [c["id"] for c in client.get("/v1/conversations").json()]
    assert ids == [first, third, second]


def test_unfavoriting_returns_to_recency_order(client: TestClient) -> None:
    first = _create(client, "first")
    second = _create(client, "second")

    _favorite(client, first, True)
    _favorite(client, first, False)

    ids = [c["id"] for c in client.get("/v1/conversations").json()]
    assert ids == [second, first]


# --- owner isolation ------------------------------------------------------------


def test_favorite_is_owner_scoped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "favorite-secret")
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

    # Bob cannot favorite a conversation he doesn't own.
    assert (
        client.put(
            f"/v1/conversations/{cid}/favorite",
            json={"favorite": True},
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )

    res = client.put(
        f"/v1/conversations/{cid}/favorite",
        json={"favorite": True},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert res.status_code == 200


# --- Duplicate / Import field parity --------------------------------------------


def test_duplicate_carries_over_favorite(client: TestClient) -> None:
    cid = _create(client, "starred")
    _favorite(client, cid, True)

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    assert res.json()["favorite"] is True


def test_duplicate_of_unfavorited_conversation_stays_unfavorited(
    client: TestClient,
) -> None:
    cid = _create(client, "plain")
    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.json()["favorite"] is False


def test_import_restores_favorite(client: TestClient) -> None:
    payload = {
        "title": "restored",
        "favorite": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.status_code == 200
    assert res.json()["favorite"] is True


def test_import_without_favorite_defaults_to_unfavorited(client: TestClient) -> None:
    payload = {
        "title": "restored",
        "messages": [{"role": "user", "content": "hi"}],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.json()["favorite"] is False


# --- database layer -------------------------------------------------------------


def test_set_conversation_favorite_returns_none_for_missing_conversation(
    db_path: object,
) -> None:
    assert database.set_conversation_favorite(999999, True) is None
