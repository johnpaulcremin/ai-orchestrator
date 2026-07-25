"""Importing a previously exported conversation (POST /v1/conversations/import).

Re-creates a conversation from scratch — fresh ids, no model calls — from
just the text of each turn. Pairs with the client-side JSON export feature.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _import(client: TestClient, body: dict, headers: dict[str, str] | None = None):
    return client.post("/v1/conversations/import", json=body, headers=headers)


def test_import_creates_conversation_and_messages_in_order(client: TestClient) -> None:
    res = _import(
        client,
        {
            "title": "Trip to Japan",
            "messages": [
                {"role": "user", "content": "any good ramen spots?"},
                {
                    "role": "assistant",
                    "content": "Try Ichiran.",
                    "mode_used": "auto->fast",
                    "notes": "n",
                },
            ],
        },
    )
    assert res.status_code == 200
    conversation = res.json()
    assert conversation["title"] == "Trip to Japan"
    cid = conversation["id"]

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert [m["content"] for m in messages] == ["any good ramen spots?", "Try Ichiran."]
    assert messages[1]["mode_used"] == "auto->fast"
    assert messages[1]["notes"] == "n"
    # Fresh ids, not whatever the export happened to carry.
    assert messages[0]["id"] != messages[1]["id"]


def test_import_defaults_title_when_omitted(client: TestClient) -> None:
    res = _import(client, {"messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200
    assert res.json()["title"] == "Imported conversation"


def test_import_rejects_empty_message_list(client: TestClient) -> None:
    res = _import(client, {"title": "t", "messages": []})
    assert res.status_code == 422


def test_import_rejects_invalid_role(client: TestClient) -> None:
    res = _import(
        client, {"title": "t", "messages": [{"role": "system", "content": "hi"}]}
    )
    assert res.status_code == 422


def test_import_rejects_empty_content(client: TestClient) -> None:
    res = _import(client, {"title": "t", "messages": [{"role": "user", "content": ""}]})
    assert res.status_code == 422


def test_import_rejects_oversized_message_content(client: TestClient) -> None:
    res = _import(
        client,
        {"title": "t", "messages": [{"role": "user", "content": "x" * 100_001}]},
    )
    assert res.status_code == 422


def test_import_rejects_too_many_messages(client: TestClient) -> None:
    res = _import(
        client,
        {
            "title": "t",
            "messages": [{"role": "user", "content": "hi"} for _ in range(501)],
        },
    )
    assert res.status_code == 422


def test_import_ignores_unknown_fields_like_images(client: TestClient) -> None:
    # An exported JSON message carries fields (id, images, files, ...) that
    # import doesn't restore; extra fields must not break the request.
    res = _import(
        client,
        {
            "title": "t",
            "messages": [
                {
                    "id": 999,
                    "conversation_id": 999,
                    "role": "user",
                    "content": "hi",
                    "images": ["data:image/png;base64,aaa"],
                    "created_at": "2020-01-01 00:00:00",
                }
            ],
        },
    )
    assert res.status_code == 200


def test_imported_conversation_is_owned_by_the_importer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "import-secret")
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

    res = _import(
        client,
        {"title": "alice's import", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {alice}"},
    )
    cid = res.json()["id"]
    assert res.json()["owner"] == "alice"

    # Bob cannot see or touch Alice's imported conversation.
    assert (
        client.get(
            f"/v1/conversations/{cid}/messages",
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )
    alice_ids = [
        c["id"]
        for c in client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {alice}"}
        ).json()
    ]
    assert cid in alice_ids
