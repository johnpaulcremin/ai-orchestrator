"""Conversation tags: freeform labels set wholesale via PUT
.../tags, normalized (trimmed, deduped, capped), and carried through
Duplicate/Import like favorite/pin/instructions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _set_tags(client: TestClient, cid: int, tags: list[str]):
    return client.put(f"/v1/conversations/{cid}/tags", json={"tags": tags})


# --- tags endpoint + persistence ------------------------------------------


def test_new_conversation_has_no_tags(client: TestClient) -> None:
    cid = _create(client)
    conv = client.get("/v1/conversations").json()[0]
    assert conv["id"] == cid
    assert conv["tags"] == []


def test_sets_and_replaces_tags(client: TestClient) -> None:
    cid = _create(client)

    res = _set_tags(client, cid, ["work", "urgent"])
    assert res.status_code == 200
    assert res.json()["tags"] == ["work", "urgent"]

    conv = client.get("/v1/conversations").json()[0]
    assert conv["tags"] == ["work", "urgent"]

    # A second call REPLACES, not merges.
    res = _set_tags(client, cid, ["personal"])
    assert res.json()["tags"] == ["personal"]


def test_clears_tags_with_an_empty_list(client: TestClient) -> None:
    cid = _create(client)
    _set_tags(client, cid, ["work"])

    res = _set_tags(client, cid, [])
    assert res.status_code == 200
    assert res.json()["tags"] == []


def test_tags_are_trimmed_deduped_and_blanks_dropped(client: TestClient) -> None:
    cid = _create(client)
    res = _set_tags(client, cid, ["  work  ", "work", "", "   ", "urgent"])
    assert res.json()["tags"] == ["work", "urgent"]


def test_tag_length_is_capped(client: TestClient) -> None:
    cid = _create(client)
    long_tag = "x" * 100
    res = _set_tags(client, cid, [long_tag])
    assert res.status_code == 200
    assert len(res.json()["tags"][0]) == 30


def test_too_many_tags_is_rejected(client: TestClient) -> None:
    cid = _create(client)
    res = _set_tags(client, cid, [f"tag{i}" for i in range(16)])
    assert res.status_code == 422


def test_tags_404_for_missing_conversation(client: TestClient) -> None:
    res = _set_tags(client, 999999, ["work"])
    assert res.status_code == 404


def test_tags_requires_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert _set_tags(client, cid, ["work"]).status_code == 401
    ok = client.put(
        f"/v1/conversations/{cid}/tags",
        json={"tags": ["work"]},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 200


def test_setting_tags_does_not_bump_updated_at(
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
    backdated = client.get("/v1/conversations").json()[0]["updated_at"]

    _set_tags(client, cid, ["work"])
    after = client.get("/v1/conversations").json()[0]["updated_at"]
    assert after == backdated


# --- owner isolation ------------------------------------------------------------


def test_tags_are_owner_scoped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "tags-secret")
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

    assert (
        client.put(
            f"/v1/conversations/{cid}/tags",
            json={"tags": ["work"]},
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )

    res = client.put(
        f"/v1/conversations/{cid}/tags",
        json={"tags": ["work"]},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert res.status_code == 200


# --- Duplicate / Import field parity --------------------------------------------


def test_duplicate_carries_over_tags(client: TestClient) -> None:
    cid = _create(client, "tagged")
    _set_tags(client, cid, ["work", "urgent"])

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    assert res.json()["tags"] == ["work", "urgent"]


def test_duplicate_of_untagged_conversation_stays_untagged(client: TestClient) -> None:
    cid = _create(client, "plain")
    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.json()["tags"] == []


def test_import_restores_tags(client: TestClient) -> None:
    payload = {
        "title": "restored",
        "tags": ["work", "urgent"],
        "messages": [{"role": "user", "content": "hi"}],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.status_code == 200
    assert res.json()["tags"] == ["work", "urgent"]


def test_import_without_tags_defaults_to_untagged(client: TestClient) -> None:
    payload = {
        "title": "restored",
        "messages": [{"role": "user", "content": "hi"}],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.json()["tags"] == []


# --- database layer -------------------------------------------------------------


def test_set_conversation_tags_returns_none_for_missing_conversation(
    db_path: object,
) -> None:
    assert database.set_conversation_tags(999999, ["work"]) is None
