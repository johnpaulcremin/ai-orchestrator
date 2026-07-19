from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_everything_open_when_token_unset(client: TestClient) -> None:
    assert client.get("/v1/conversations").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/v1/status").status_code == 200
    assert client.get("/v1/status").json()["auth_enabled"] is False


def test_missing_header_rejected_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")

    response = client.get("/v1/conversations")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")

    response = client.get(
        "/v1/conversations",
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


def test_correct_token_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")

    response = client.get(
        "/v1/conversations",
        headers={"Authorization": "Bearer sekret"},
    )

    assert response.status_code == 200


def test_health_and_status_stay_open_with_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")

    assert client.get("/health").status_code == 200

    status = client.get("/v1/status")
    assert status.status_code == 200
    assert status.json()["auth_enabled"] is True


def test_non_ascii_bearer_token_rejected_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Starlette latin-1-decodes header bytes, so a raw non-ASCII Authorization
    # value reaches require_api_token as a non-ASCII str. It must raise a clean
    # 401 (HTTPException), not a TypeError from secrets.compare_digest.
    from fastapi import HTTPException

    from app.auth import require_api_token

    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")

    with pytest.raises(HTTPException) as exc_info:
        require_api_token(authorization="Bearer \xf1o\xf1o-caf\xe9")

    assert exc_info.value.status_code == 401
