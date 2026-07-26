"""Archive conversations: hides a conversation from the default sidebar
list (recoverable) without deleting it — distinct from DELETE, which is
permanent. Deliberately NOT copied by Duplicate/Import, unlike favorite/pin/
instructions: a duplicate or a restored import is a fresh working copy, and
starting it pre-archived would defeat the point of either action.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _archive(client: TestClient, cid: int, archived: bool):
    return client.put(f"/v1/conversations/{cid}/archive", json={"archived": archived})


# --- archive endpoint + persistence -------------------------------------------


def test_new_conversation_is_not_archived(client: TestClient) -> None:
    cid = _create(client)
    conv = client.get("/v1/conversations").json()[0]
    assert conv["id"] == cid
    assert conv["archived"] is False


def test_archive_set_and_cleared(client: TestClient) -> None:
    cid = _create(client)

    res = _archive(client, cid, True)
    assert res.status_code == 200
    assert res.json()["archived"] is True

    res = _archive(client, cid, False)
    assert res.status_code == 200
    assert res.json()["archived"] is False


def test_archive_404_for_missing_conversation(client: TestClient) -> None:
    res = _archive(client, 999999, True)
    assert res.status_code == 404


def test_archive_requires_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert _archive(client, cid, True).status_code == 401
    ok = client.put(
        f"/v1/conversations/{cid}/archive",
        json={"archived": True},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 200


def test_archiving_does_not_bump_updated_at(
    client: TestClient, db_path: object
) -> None:
    import sqlite3

    cid = _create(client)

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE conversations SET updated_at = datetime('now', '-1 hour') WHERE id = ?",
        (cid,),
    )
    conn.commit()
    conn.close()
    backdated = client.get(
        "/v1/conversations", params={"include_archived": "true"}
    ).json()[0]["updated_at"]

    _archive(client, cid, True)
    after = client.get("/v1/conversations", params={"include_archived": "true"}).json()[
        0
    ]["updated_at"]
    assert after == backdated


# --- default list visibility ----------------------------------------------------


def test_archived_conversation_is_hidden_from_the_default_list(
    client: TestClient,
) -> None:
    visible = _create(client, "visible")
    hidden = _create(client, "hidden")
    _archive(client, hidden, True)

    ids = [c["id"] for c in client.get("/v1/conversations").json()]
    assert ids == [visible]


def test_include_archived_shows_everything(client: TestClient) -> None:
    visible = _create(client, "visible")
    hidden = _create(client, "hidden")
    _archive(client, hidden, True)

    res = client.get("/v1/conversations", params={"include_archived": "true"})
    ids = {c["id"] for c in res.json()}
    assert ids == {visible, hidden}


def test_unarchiving_makes_it_visible_again(client: TestClient) -> None:
    cid = _create(client)
    _archive(client, cid, True)
    assert client.get("/v1/conversations").json() == []

    _archive(client, cid, False)
    ids = [c["id"] for c in client.get("/v1/conversations").json()]
    assert ids == [cid]


def test_archived_conversation_is_still_directly_reachable(client: TestClient) -> None:
    # Archiving hides it from the list, not from direct access (messages,
    # ask, etc.) — it's a visibility filter, not a lock.
    cid = _create(client, "hidden")
    _archive(client, cid, True)

    assert client.get(f"/v1/conversations/{cid}/messages").status_code == 200


# --- owner isolation ------------------------------------------------------------


def test_archive_is_owner_scoped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "archive-secret")
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

    # Bob cannot archive a conversation he doesn't own.
    assert (
        client.put(
            f"/v1/conversations/{cid}/archive",
            json={"archived": True},
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )

    res = client.put(
        f"/v1/conversations/{cid}/archive",
        json={"archived": True},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert res.status_code == 200


def test_archived_list_is_scoped_to_the_caller_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "archive-secret-2")
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

    client.post(
        "/v1/conversations",
        json={"title": "alice's chat"},
        headers={"Authorization": f"Bearer {alice}"},
    )

    res = client.get(
        "/v1/conversations",
        params={"include_archived": "true"},
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.json() == []


# --- Duplicate / Import do NOT carry over archived status -----------------------


def test_duplicate_of_archived_conversation_is_not_archived(
    client: TestClient,
) -> None:
    cid = _create(client, "archived source")
    _archive(client, cid, True)

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    assert res.json()["archived"] is False


def test_import_never_produces_an_archived_conversation(client: TestClient) -> None:
    payload = {
        "title": "restored",
        "messages": [{"role": "user", "content": "hi"}],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.json()["archived"] is False


# --- database layer -------------------------------------------------------------


def test_set_conversation_archived_returns_none_for_missing_conversation(
    db_path: object,
) -> None:
    assert database.set_conversation_archived(999999, True) is None
