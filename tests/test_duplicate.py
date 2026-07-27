"""Duplicating a conversation (POST /v1/conversations/{id}/duplicate).

A server-side, full-fidelity copy (title, pin, instructions, every message
including attachments/cost/tokens) into a brand-new conversation — distinct
from import, which is a client-JSON, text-only re-creation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def test_duplicate_copies_title_pin_and_instructions(client: TestClient) -> None:
    cid = _create(client, "Trip to Japan")
    client.put(f"/v1/conversations/{cid}/pin", json={"model": "claude-sonnet-5"})
    client.put(
        f"/v1/conversations/{cid}/system_prompt", json={"system_prompt": "Be terse."}
    )

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    copy = res.json()
    assert copy["id"] != cid
    assert copy["title"] == "Trip to Japan (copy)"
    assert copy["pinned_model"] == "claude-sonnet-5"
    assert copy["system_prompt"] == "Be terse."


def test_duplicate_copies_messages_in_order_with_full_fidelity(
    client: TestClient, db_path: Path
) -> None:
    cid = _create(client)
    database.add_message(
        cid,
        role="user",
        content="any good ramen spots?",
        images=json.dumps(["data:image/png;base64,aaa"]),
    )
    database.add_message(
        cid,
        role="assistant",
        content="Try Ichiran.",
        mode_used="auto->fast",
        notes="n",
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.01,
        cached=True,
        sources=json.dumps([{"title": "Ichiran", "url": "https://ichiran.example"}]),
        truncated=True,
        code_results=json.dumps([{"code": "print(1)", "logs": "1", "images": []}]),
    )

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    copy_id = res.json()["id"]

    messages = client.get(f"/v1/conversations/{copy_id}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "any good ramen spots?"
    assert messages[0]["images"] == ["data:image/png;base64,aaa"]
    assert messages[1]["content"] == "Try Ichiran."
    assert messages[1]["mode_used"] == "auto->fast"
    assert messages[1]["notes"] == "n"
    assert messages[1]["input_tokens"] == 10
    assert messages[1]["output_tokens"] == 20
    assert messages[1]["cost_usd"] == 0.01
    assert messages[1]["cached"] is True
    assert messages[1]["sources"] == [
        {"title": "Ichiran", "url": "https://ichiran.example"}
    ]
    assert messages[1]["truncated"] is True
    assert messages[1]["code_results"] == [
        {"code": "print(1)", "logs": "1", "images": []}
    ]
    # Fresh ids, not the originals'.
    original_ids = {
        m["id"] for m in client.get(f"/v1/conversations/{cid}/messages").json()
    }
    assert not original_ids & {m["id"] for m in messages}


def test_duplicate_does_not_copy_a_pending_action(
    client: TestClient, db_path: Path
) -> None:
    cid = _create(client)
    database.add_message(
        cid,
        role="assistant",
        content="I'll send that email.",
        pending_action=json.dumps(
            {"action": "send_email", "summary": "s", "payload": {"to": "x"}}
        ),
        action_status="pending",
    )

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    copy_id = res.json()["id"]

    messages = client.get(f"/v1/conversations/{copy_id}/messages").json()
    assert messages[0]["pending_action"] is None
    assert messages[0]["action_status"] is None


def test_duplicate_of_empty_conversation_has_no_messages(client: TestClient) -> None:
    cid = _create(client, "empty")
    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    copy_id = res.json()["id"]
    assert client.get(f"/v1/conversations/{copy_id}/messages").json() == []


def test_duplicate_nonexistent_conversation_is_404(client: TestClient) -> None:
    res = client.post("/v1/conversations/999999/duplicate")
    assert res.status_code == 404


def test_duplicate_is_owned_by_the_duplicator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "duplicate-secret")
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

    # Bob cannot duplicate a conversation he doesn't own.
    assert (
        client.post(
            f"/v1/conversations/{cid}/duplicate",
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )

    res = client.post(
        f"/v1/conversations/{cid}/duplicate",
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert res.status_code == 200
    assert res.json()["owner"] == "alice"
