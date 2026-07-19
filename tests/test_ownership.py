from __future__ import annotations

import pytest

from app import main
from app.schemas import AskResponse

JWT_SECRET = "iso-secret"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


def _register_login(client, username: str, password: str = "password123") -> str:
    client.post("/v1/auth/register", json={"username": username, "password": password})
    resp = client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    return resp.json()["access_token"]


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_conversations_isolated_per_user(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    conv = client.post(
        "/v1/conversations", json={"title": "alice chat"}, headers=_hdr(alice)
    ).json()
    assert conv["owner"] == "alice"
    cid = conv["id"]

    alice_ids = [
        c["id"] for c in client.get("/v1/conversations", headers=_hdr(alice)).json()
    ]
    bob_ids = [
        c["id"] for c in client.get("/v1/conversations", headers=_hdr(bob)).json()
    ]
    assert cid in alice_ids
    assert cid not in bob_ids

    # Bob cannot touch Alice's conversation — 404 hides its existence.
    assert (
        client.get(f"/v1/conversations/{cid}/messages", headers=_hdr(bob)).status_code
        == 404
    )
    assert (
        client.patch(
            f"/v1/conversations/{cid}", json={"title": "hijack"}, headers=_hdr(bob)
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/conversations/{cid}", headers=_hdr(bob)).status_code == 404
    )

    # Alice can.
    assert (
        client.get(f"/v1/conversations/{cid}/messages", headers=_hdr(alice)).status_code
        == 200
    )


def test_ask_and_stream_enforce_ownership(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    monkeypatch.setattr(
        main,
        "run_orchestrator",
        lambda req: AskResponse(answer="x", mode_used="fast", notes="n"),
    )
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")
    cid = client.post(
        "/v1/conversations", json={"title": "x"}, headers=_hdr(alice)
    ).json()["id"]

    assert (
        client.post(
            f"/v1/conversations/{cid}/ask", json={"question": "hi"}, headers=_hdr(bob)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask/stream",
            json={"question": "hi"},
            headers=_hdr(bob),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask", json={"question": "hi"}, headers=_hdr(alice)
        ).status_code
        == 200
    )


def test_me_endpoint_reports_user(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    assert client.get("/v1/auth/me", headers=_hdr(alice)).json()["username"] == "alice"


def test_conversations_shared_when_auth_off(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    conv = client.post("/v1/conversations", json={"title": "shared"}).json()
    assert conv["owner"] is None
    listed = [c["id"] for c in client.get("/v1/conversations").json()]
    assert conv["id"] in listed
