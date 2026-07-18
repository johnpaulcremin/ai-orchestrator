from __future__ import annotations

import pytest

JWT_SECRET = "test-secret-key"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


def test_register_login_and_access(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)

    reg = client.post(
        "/v1/auth/register", json={"username": "alice", "password": "supersecret"}
    )
    assert reg.status_code == 201
    assert reg.json()["username"] == "alice"

    login = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "supersecret"}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    assert token

    ok = client.get("/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    assert client.get("/v1/conversations").status_code == 401
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": "Bearer garbage"}
        ).status_code
        == 401
    )


def test_login_wrong_password(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )

    bad = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "wrongpass"}
    )
    assert bad.status_code == 401


def test_duplicate_registration_conflicts(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    client.post(
        "/v1/auth/register", json={"username": "carol", "password": "password123"}
    )
    dupe = client.post(
        "/v1/auth/register", json={"username": "carol", "password": "password123"}
    )
    assert dupe.status_code == 409


def test_registration_can_be_disabled(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    resp = client.post(
        "/v1/auth/register", json={"username": "dave", "password": "password123"}
    )
    assert resp.status_code == 403


def test_auth_endpoints_need_jwt_enabled(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    assert (
        client.post(
            "/v1/auth/register", json={"username": "eve", "password": "password123"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/auth/login", json={"username": "eve", "password": "password123"}
        ).status_code
        == 400
    )


def test_static_token_and_jwt_coexist(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("API_AUTH_TOKEN", "static-tok")

    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": "Bearer static-tok"}
        ).status_code
        == 200
    )

    client.post(
        "/v1/auth/register", json={"username": "frank", "password": "password123"}
    )
    token = client.post(
        "/v1/auth/login", json={"username": "frank", "password": "password123"}
    ).json()["access_token"]
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 200
    )


def test_status_reports_jwt_state(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    body = client.get("/v1/status").json()
    assert body["auth_enabled"] is True
    assert body["jwt_enabled"] is True
    assert body["registration_allowed"] is True
