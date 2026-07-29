"""Read-only conversation share links (app/routers/shares.py + the
share_tokens table in app/database.py): owner-gated generate/status/revoke,
plus the genuinely public GET /v1/shared/{token} view.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, ratelimit


def _create(client: TestClient) -> int:
    return int(
        client.post("/v1/conversations", json={"title": "Shared chat"}).json()["id"]
    )


# --- status / create / revoke (owner-gated) --------------------------------------


def test_share_status_starts_inactive(client: TestClient) -> None:
    cid = _create(client)
    status = client.get(f"/v1/conversations/{cid}/share").json()
    assert status == {"active": False, "token": None, "expires_at": None}


def test_create_share_returns_an_active_token(client: TestClient) -> None:
    cid = _create(client)
    created = client.post(f"/v1/conversations/{cid}/share", json={}).json()
    assert created["active"] is True
    assert isinstance(created["token"], str) and len(created["token"]) > 10
    assert created["expires_at"] is None

    status = client.get(f"/v1/conversations/{cid}/share").json()
    assert status == created


def test_create_share_with_ttl_sets_an_expiry(client: TestClient) -> None:
    cid = _create(client)
    created = client.post(
        f"/v1/conversations/{cid}/share", json={"ttl_hours": 24}
    ).json()
    assert created["active"] is True
    assert created["expires_at"] is not None


@pytest.mark.parametrize("ttl_hours", [0, -1, 8761])
def test_create_share_rejects_out_of_range_ttl(
    client: TestClient, ttl_hours: int
) -> None:
    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/share", json={"ttl_hours": ttl_hours})
    assert r.status_code == 422


def test_regenerating_a_share_link_invalidates_the_old_token(
    client: TestClient,
) -> None:
    cid = _create(client)
    first = client.post(f"/v1/conversations/{cid}/share", json={}).json()
    second = client.post(f"/v1/conversations/{cid}/share", json={}).json()

    assert first["token"] != second["token"]
    assert client.get(f"/v1/shared/{first['token']}").status_code == 404
    assert client.get(f"/v1/shared/{second['token']}").status_code == 200


def test_revoke_share_deactivates_the_link(client: TestClient) -> None:
    cid = _create(client)
    created = client.post(f"/v1/conversations/{cid}/share", json={}).json()

    revoked = client.delete(f"/v1/conversations/{cid}/share").json()
    assert revoked == {"active": False, "token": None, "expires_at": None}
    assert client.get(f"/v1/shared/{created['token']}").status_code == 404


def test_revoke_share_is_a_noop_when_never_shared(client: TestClient) -> None:
    cid = _create(client)
    r = client.delete(f"/v1/conversations/{cid}/share")
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_share_endpoints_404_for_a_nonexistent_conversation(client: TestClient) -> None:
    assert client.get("/v1/conversations/999/share").status_code == 404
    assert client.post("/v1/conversations/999/share", json={}).status_code == 404
    assert client.delete("/v1/conversations/999/share").status_code == 404


def test_share_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get(f"/v1/conversations/{cid}/share").status_code == 401
    assert client.post(f"/v1/conversations/{cid}/share", json={}).status_code == 401
    assert client.delete(f"/v1/conversations/{cid}/share").status_code == 401

    auth = {"Authorization": "Bearer secret-token"}
    assert client.get(f"/v1/conversations/{cid}/share", headers=auth).status_code == 200


def test_share_endpoints_are_owner_scoped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One JWT-authenticated user can't see, share, or revoke another's
    conversation's link — same 404-not-403 convention as every other
    conversation-scoped endpoint."""
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")

    def _token(username: str) -> dict[str, str]:
        client.post(
            "/v1/auth/register", json={"username": username, "password": "pw123456"}
        )
        token = client.post(
            "/v1/auth/login", json={"username": username, "password": "pw123456"}
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    alice = _token("alice")
    bob = _token("bob")

    cid = int(
        client.post("/v1/conversations", json={"title": "t"}, headers=alice).json()[
            "id"
        ]
    )

    assert client.get(f"/v1/conversations/{cid}/share", headers=bob).status_code == 404
    assert (
        client.post(f"/v1/conversations/{cid}/share", json={}, headers=bob).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/conversations/{cid}/share", headers=bob).status_code == 404
    )
    # The owner themselves can, of course.
    assert (
        client.get(f"/v1/conversations/{cid}/share", headers=alice).status_code == 200
    )


# --- GET /v1/shared/{token}: the public view --------------------------------------


def test_shared_view_needs_no_auth_even_when_api_auth_token_is_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = _create(client)
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    token = client.post(
        f"/v1/conversations/{cid}/share",
        json={},
        headers={"Authorization": "Bearer secret-token"},
    ).json()["token"]

    # No Authorization header at all -- this is the whole point of the feature.
    r = client.get(f"/v1/shared/{token}")
    assert r.status_code == 200


def test_shared_view_returns_title_and_messages(client: TestClient) -> None:
    cid = _create(client)
    database.add_message(conversation_id=cid, role="user", content="hello")
    database.add_message(conversation_id=cid, role="assistant", content="hi there")
    token = client.post(f"/v1/conversations/{cid}/share", json={}).json()["token"]

    r = client.get(f"/v1/shared/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Shared chat"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in body["messages"]] == ["hello", "hi there"]


def test_shared_view_omits_cost_and_internal_fields(client: TestClient) -> None:
    """A share recipient must never see the owner's spend, which model
    answered, or internal notes -- only the conversational content."""
    cid = _create(client)
    database.add_message(
        conversation_id=cid,
        role="assistant",
        content="answer",
        mode_used="auto->smart",
        notes="internal routing notes",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.05,
    )
    token = client.post(f"/v1/conversations/{cid}/share", json={}).json()["token"]

    message = client.get(f"/v1/shared/{token}").json()["messages"][0]
    for leaked_field in (
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "mode_used",
        "notes",
        "cached",
        "bookmarked",
        "pending_action",
        "action_status",
    ):
        assert leaked_field not in message


def test_shared_view_includes_images_files_sources(client: TestClient) -> None:
    cid = _create(client)
    database.add_message(
        conversation_id=cid,
        role="assistant",
        content="see attached",
        sources='[{"title": "Example", "url": "https://example.com"}]',
    )
    token = client.post(f"/v1/conversations/{cid}/share", json={}).json()["token"]

    message = client.get(f"/v1/shared/{token}").json()["messages"][0]
    assert message["sources"] == [{"title": "Example", "url": "https://example.com"}]


def test_shared_view_404s_for_an_unknown_token(client: TestClient) -> None:
    r = client.get("/v1/shared/not-a-real-token")
    assert r.status_code == 404


def test_shared_view_404s_for_an_expired_token(
    client: TestClient, db_path: Path
) -> None:
    cid = _create(client)
    token = client.post(f"/v1/conversations/{cid}/share", json={"ttl_hours": 1}).json()[
        "token"
    ]

    # Backdate expires_at into the past directly (see test_search.py's same
    # pattern for SQLite's 1-second CURRENT_TIMESTAMP resolution).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE share_tokens SET expires_at = '2000-01-01 00:00:00' WHERE token = ?",
            (token,),
        )

    assert client.get(f"/v1/shared/{token}").status_code == 404
    assert client.get(f"/v1/conversations/{cid}/share").json()["active"] is False


def test_deleting_the_conversation_invalidates_its_share_link(
    client: TestClient,
) -> None:
    cid = _create(client)
    token = client.post(f"/v1/conversations/{cid}/share", json={}).json()["token"]
    client.delete(f"/v1/conversations/{cid}")
    assert client.get(f"/v1/shared/{token}").status_code == 404


def test_shared_endpoint_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public share view sits behind the always-on auth_limiter, the same
    brute-force guard login/register get -- distinct from the opt-in
    RATE_LIMIT ask-endpoint limiter."""
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.auth_limiter, "enabled", True)
    try:
        ratelimit.auth_limiter.reset()
    except Exception:
        pass

    first = client.get("/v1/shared/token-a")
    second = client.get("/v1/shared/token-b")
    third = client.get("/v1/shared/token-c")

    assert first.status_code == 404  # unknown token, but not rate-limited yet
    assert second.status_code == 404
    assert third.status_code == 429


# --- database layer ----------------------------------------------------------------


def test_create_share_token_replaces_any_existing_row(db_path: Path) -> None:
    conv = database.create_conversation("t", None)
    cid = int(conv["id"])
    database.create_share_token(cid, "token-1", None)
    database.create_share_token(cid, "token-2", None)

    assert database.get_conversation_id_by_token("token-1") is None
    assert database.get_conversation_id_by_token("token-2") == cid


def test_delete_share_tokens_reports_whether_anything_was_removed(
    db_path: Path,
) -> None:
    conv = database.create_conversation("t", None)
    cid = int(conv["id"])
    assert database.delete_share_tokens(cid) == 0

    database.create_share_token(cid, "token-1", None)
    assert database.delete_share_tokens(cid) == 1
    assert database.get_share_token(cid) is None
