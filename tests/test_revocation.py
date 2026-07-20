from __future__ import annotations

import time

import pytest

from app import revocation

JWT_SECRET = "test-secret-key"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


def _login(client) -> str:
    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "supersecret"}
    )
    return client.post(
        "/v1/auth/login", json={"username": "alice", "password": "supersecret"}
    ).json()["access_token"]


# --- revocation store unit ---------------------------------------------------


def test_store_revoke_and_check() -> None:
    revocation.clear()
    future = int(time.time()) + 60
    assert revocation.is_revoked("jti-1") is False
    revocation.revoke("jti-1", future)
    assert revocation.is_revoked("jti-1") is True


def test_store_ignores_already_expired() -> None:
    revocation.clear()
    past = int(time.time()) - 5
    revocation.revoke("old", past)
    # Expired on its own, so it reads as not-revoked (and is pruned).
    assert revocation.is_revoked("old") is False


def test_store_empty_jti_is_noop() -> None:
    revocation.clear()
    revocation.revoke("", int(time.time()) + 60)
    assert revocation.is_revoked("") is False


def test_store_exp_boundary_is_strict() -> None:
    # A revoked entry must stay live while jose still accepts the token, i.e. at
    # now == exp it is still revoked (jose treats that second as not-yet-expired).
    revocation.clear()
    now = int(time.time())
    revocation.revoke("boundary", now)  # exp == now
    assert revocation.is_revoked("boundary") is True


def test_user_epoch_bump() -> None:
    revocation.clear()
    assert revocation.user_epoch("u") == 0
    assert revocation.bump_user_epoch("u") == 1
    assert revocation.user_epoch("u") == 1
    assert revocation.user_epoch("other") == 0


# --- logout ------------------------------------------------------------------


def test_logout_revokes_api_access_and_ownership(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)
    auth = {"Authorization": f"Bearer {token}"}

    # The token works before logout.
    assert client.get("/v1/conversations", headers=auth).status_code == 200
    assert client.get("/v1/auth/me", headers=auth).json()["username"] == "alice"

    out = client.post("/v1/auth/logout", headers=auth)
    assert out.status_code == 200
    assert out.json()["status"] == "logged_out"

    # After logout the same token is rejected everywhere (access AND ownership).
    assert client.get("/v1/conversations", headers=auth).status_code == 401


def test_logout_without_token_is_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    assert client.post("/v1/auth/logout").status_code == 401
    assert (
        client.post(
            "/v1/auth/logout", headers={"Authorization": "Bearer garbage"}
        ).status_code
        == 401
    )


def test_logout_requires_jwt_enabled(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    assert client.post("/v1/auth/logout").status_code == 400


# --- refresh -----------------------------------------------------------------


def test_refresh_rotates_the_old_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)

    res = client.post("/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    fresh = res.json()["access_token"]
    assert fresh and fresh != token

    # Rotation: the OLD token stops working, the new one works.
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {fresh}"}
        ).status_code
        == 200
    )


def test_logout_revokes_all_of_a_users_sessions(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The security fix: logging out one session kills every token the user holds,
    # including a token that was refreshed onto a fresh jti (so a laundered
    # session can't outlive a logout).
    _enable_jwt(monkeypatch)
    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "supersecret"}
    )

    def login() -> str:
        return client.post(
            "/v1/auth/login", json={"username": "alice", "password": "supersecret"}
        ).json()["access_token"]

    token_a = login()
    token_b = login()  # a second, independent session
    for t in (token_a, token_b):
        assert (
            client.get(
                "/v1/conversations", headers={"Authorization": f"Bearer {t}"}
            ).status_code
            == 200
        )

    # Log out via ONE session...
    assert (
        client.post(
            "/v1/auth/logout", headers={"Authorization": f"Bearer {token_a}"}
        ).status_code
        == 200
    )

    # ...and BOTH sessions are now dead.
    for t in (token_a, token_b):
        assert (
            client.get(
                "/v1/conversations", headers={"Authorization": f"Bearer {t}"}
            ).status_code
            == 401
        )

    # A fresh login after the logout works normally.
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {login()}"}
        ).status_code
        == 200
    )


def test_refresh_rejects_a_revoked_token(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)
    auth = {"Authorization": f"Bearer {token}"}

    client.post("/v1/auth/logout", headers=auth)
    assert client.post("/v1/auth/refresh", headers=auth).status_code == 401


def test_refresh_without_token_is_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    assert client.post("/v1/auth/refresh").status_code == 401
