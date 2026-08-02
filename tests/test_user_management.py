from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _register_login(
    client: TestClient, username: str, password: str = "password123"
) -> str:
    client.post("/v1/auth/register", json={"username": username, "password": password})
    return _login(client, username, password)["access_token"]


def _admin_env(monkeypatch: pytest.MonkeyPatch, admins: str = "root") -> None:
    monkeypatch.setenv("JWT_SECRET", "user-mgmt-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_USERNAMES", admins)
    # Registration must stay closed throughout this feature's operation —
    # the owner provisions accounts himself via the admin API.
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")


def _bootstrap_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> str:
    """Provision the admin account itself while registration is briefly
    open (a realistic operator flow), then close registration for the rest
    of the test — matching test_settings.py's pattern."""
    monkeypatch.setenv("JWT_SECRET", "user-mgmt-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_USERNAMES", "root")
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    token = _register_login(client, "root")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    return token


# --- Admin gate: every endpoint 403s for non-admins and anonymous -----------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/users"),
        ("POST", "/v1/users"),
        ("POST", "/v1/users/someone/reset-password"),
        ("POST", "/v1/users/someone/deactivate"),
        ("POST", "/v1/users/someone/reactivate"),
    ],
)
def test_user_endpoints_401_for_anonymous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    # No bearer token at all fails the API-auth gate itself (401) before the
    # admin check ever runs — same convention as every other /v1 endpoint
    # (see test_settings_endpoints_require_auth). A *logged-in* non-admin is
    # the 403 case, covered by test_user_endpoints_403_for_non_admin below.
    _admin_env(monkeypatch)
    res = client.request(
        method, path, json={"username": "x"} if method == "POST" else None
    )
    assert res.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/users"),
        ("POST", "/v1/users"),
        ("POST", "/v1/users/someone/reset-password"),
        ("POST", "/v1/users/someone/deactivate"),
        ("POST", "/v1/users/someone/reactivate"),
    ],
)
def test_user_endpoints_403_for_non_admin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    # Admin creates a regular user to act as the non-admin caller.
    created = client.post(
        "/v1/users", json={"username": "family1"}, headers=_hdr(admin_token)
    )
    assert created.status_code == 201
    temp_password = created.json()["temporary_password"]
    other_token = _login(client, "family1", temp_password)["access_token"]

    res = client.request(
        method,
        path,
        json={"username": "x"} if method == "POST" else None,
        headers=_hdr(other_token),
    )
    assert res.status_code == 403


def test_user_endpoints_403_when_admin_usernames_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even the operator's own token is denied when nobody is configured as
    # an admin — user management simply isn't usable yet.
    monkeypatch.setenv("JWT_SECRET", "user-mgmt-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_USERNAMES", raising=False)
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    token = _register_login(client, "owner")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    res = client.get("/v1/users", headers=_hdr(token))
    assert res.status_code == 403


# --- Create / list ------------------------------------------------------


def test_admin_create_user_returns_temp_password_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)

    res = client.post(
        "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
    )
    assert res.status_code == 201
    body = res.json()
    assert body["user"]["username"] == "grandma"
    assert body["user"]["must_change_password"] is True
    assert body["user"]["is_active"] is True
    assert len(body["temporary_password"]) >= 8

    listing = client.get("/v1/users", headers=_hdr(admin_token)).json()
    usernames = {u["username"] for u in listing}
    assert {"root", "grandma"} <= usernames


def test_admin_create_user_duplicate_username_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    client.post("/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token))
    dup = client.post(
        "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
    )
    assert dup.status_code == 409


def test_registration_stays_closed_throughout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    client.post("/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token))

    reg = client.post(
        "/v1/auth/register", json={"username": "sneaky", "password": "password123"}
    )
    assert reg.status_code == 403


# --- must_change_password flow -------------------------------------------


def test_temp_password_account_forced_through_change_flow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    created = client.post(
        "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
    )
    temp_password = created.json()["temporary_password"]

    login = _login(client, "grandma", temp_password)
    assert login["must_change_password"] is True
    token = login["access_token"]

    me = client.get("/v1/auth/me", headers=_hdr(token)).json()
    assert me["must_change_password"] is True
    assert me["is_admin"] is False

    # Wrong current password doesn't clear the flag.
    bad = client.post(
        "/v1/auth/change-password",
        json={"current_password": "wrong", "new_password": "newpassword123"},
        headers=_hdr(token),
    )
    assert bad.status_code == 401
    assert (
        client.get("/v1/auth/me", headers=_hdr(token)).json()["must_change_password"]
        is True
    )

    ok = client.post(
        "/v1/auth/change-password",
        json={"current_password": temp_password, "new_password": "newpassword123"},
        headers=_hdr(token),
    )
    assert ok.status_code == 200
    assert ok.json()["must_change_password"] is False

    # The flag stays cleared, and the new password now works for login.
    assert (
        client.get("/v1/auth/me", headers=_hdr(token)).json()["must_change_password"]
        is False
    )
    relogin = _login(client, "grandma", "newpassword123")
    assert relogin["must_change_password"] is False


def test_change_password_requires_login(client: TestClient) -> None:
    res = client.post(
        "/v1/auth/change-password",
        json={"current_password": "a", "new_password": "newpassword123"},
    )
    assert res.status_code == 400


def test_admin_reset_password_flags_must_change_and_revokes_sessions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    created = client.post(
        "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
    )
    first_password = created.json()["temporary_password"]
    first_login = _login(client, "grandma", first_password)
    old_token = first_login["access_token"]

    reset = client.post("/v1/users/grandma/reset-password", headers=_hdr(admin_token))
    assert reset.status_code == 200
    new_password = reset.json()["temporary_password"]
    assert new_password != first_password

    # The old session is revoked immediately — the JWT is well-formed but
    # its jti/epoch check fails, so the API-auth gate itself rejects it.
    assert client.get("/v1/auth/me", headers=_hdr(old_token)).status_code == 401

    # Old password no longer works; new temp password does and is flagged.
    assert (
        client.post(
            "/v1/auth/login", json={"username": "grandma", "password": first_password}
        ).status_code
        == 401
    )
    relogin = _login(client, "grandma", new_password)
    assert relogin["must_change_password"] is True


def test_reset_password_unknown_user_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    res = client.post(
        "/v1/users/does-not-exist/reset-password", headers=_hdr(admin_token)
    )
    assert res.status_code == 404


# --- Deactivate / reactivate ----------------------------------------------


def test_deactivated_user_cannot_sign_in_but_conversations_survive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    created = client.post(
        "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
    )
    temp_password = created.json()["temporary_password"]
    login = _login(client, "grandma", temp_password)
    token = login["access_token"]
    client.post(
        "/v1/auth/change-password",
        json={"current_password": temp_password, "new_password": "newpassword123"},
        headers=_hdr(token),
    )

    made = client.post(
        "/v1/conversations", json={"title": "Family recipes"}, headers=_hdr(token)
    )
    assert made.status_code == 200
    conversation_id = made.json()["id"]

    deactivate = client.post("/v1/users/grandma/deactivate", headers=_hdr(admin_token))
    assert deactivate.status_code == 200
    assert deactivate.json()["is_active"] is False

    # Existing session revoked immediately; can't sign in again either.
    assert client.get("/v1/auth/me", headers=_hdr(token)).status_code == 401
    denied = client.post(
        "/v1/auth/login", json={"username": "grandma", "password": "newpassword123"}
    )
    assert denied.status_code == 401

    reactivate = client.post("/v1/users/grandma/reactivate", headers=_hdr(admin_token))
    assert reactivate.status_code == 200
    assert reactivate.json()["is_active"] is True

    relogin = _login(client, "grandma", "newpassword123")
    new_token = relogin["access_token"]
    listing = client.get("/v1/conversations", headers=_hdr(new_token)).json()
    assert any(
        c["id"] == conversation_id and c["title"] == "Family recipes" for c in listing
    )


def test_deactivate_reactivate_unknown_user_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)
    assert (
        client.post(
            "/v1/users/does-not-exist/deactivate", headers=_hdr(admin_token)
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/users/does-not-exist/reactivate", headers=_hdr(admin_token)
        ).status_code
        == 404
    )


# --- Solo behaviour pinned -------------------------------------------------


def test_solo_settings_behaviour_unchanged_when_admin_usernames_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "user-mgmt-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_USERNAMES", raising=False)
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    token = _register_login(client, "owner")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    res = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(token)
    )
    assert res.status_code == 200

    settings_view = client.get("/v1/settings", headers=_hdr(token)).json()
    assert settings_view["admin_gated"] is False
    assert settings_view["editable"] is True


def test_settings_gated_when_admin_usernames_set_even_if_registration_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # New behaviour (requirement 3): ADMIN_USERNAMES non-empty gates Settings
    # regardless of registration state — previously this only gated while
    # registration was open.
    admin_token = _bootstrap_admin(client, monkeypatch)
    created = client.post(
        "/v1/users", json={"username": "family1"}, headers=_hdr(admin_token)
    )
    temp_password = created.json()["temporary_password"]
    other_token = _login(client, "family1", temp_password)["access_token"]

    blocked = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(other_token)
    )
    assert blocked.status_code == 403

    other_view = client.get("/v1/settings", headers=_hdr(other_token)).json()
    assert other_view["admin_gated"] is True
    assert other_view["is_admin"] is False
    assert other_view["editable"] is False

    admin_view = client.get("/v1/settings", headers=_hdr(admin_token)).json()
    assert admin_view["is_admin"] is True
    assert admin_view["editable"] is True

    ok = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(admin_token)
    )
    assert ok.status_code == 200


# --- Temp passwords never logged -------------------------------------------


def test_temp_password_never_logged_on_create_and_reset(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin_token = _bootstrap_admin(client, monkeypatch)

    with caplog.at_level(logging.DEBUG):
        created = client.post(
            "/v1/users", json={"username": "grandma"}, headers=_hdr(admin_token)
        )
        temp_password = created.json()["temporary_password"]

        reset = client.post(
            "/v1/users/grandma/reset-password", headers=_hdr(admin_token)
        )
        new_temp_password = reset.json()["temporary_password"]

    log_text = caplog.text
    assert temp_password not in log_text
    assert new_temp_password not in log_text
