"""Client crash-report intake (POST /v1/client-errors, public) and review
(GET /v1/client-errors, authed) — see app/routers/system.py and
frontend/src/crashReporter.ts. The intake must work UNauthenticated (the
report matters most when the app crashed before login), but hardened:
truncation-on-store, bounded row count, user agent from the header.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database


def test_report_stored_and_listed_newest_first(client: TestClient) -> None:
    for n in (1, 2):
        response = client.post(
            "/v1/client-errors",
            json={
                "message": f"TypeError: boom {n}",
                "stack": f"at App.tsx:{n}",
                "source_url": "https://device.ts.net/",
            },
            headers={"User-Agent": "iPhone-Safari/15.0"},
        )
        assert response.status_code == 204

    listed = client.get("/v1/client-errors").json()["errors"]
    assert [e["message"] for e in listed[:2]] == [
        "TypeError: boom 2",
        "TypeError: boom 1",
    ]
    assert listed[0]["stack"] == "at App.tsx:2"
    assert listed[0]["source_url"] == "https://device.ts.net/"
    assert listed[0]["user_agent"] == "iPhone-Safari/15.0"
    assert listed[0]["created_at"]


def test_report_optional_fields_default_to_null(client: TestClient) -> None:
    response = client.post("/v1/client-errors", json={"message": "bare minimum"})
    assert response.status_code == 204
    newest = client.get("/v1/client-errors").json()["errors"][0]
    assert newest["stack"] is None
    assert newest["source_url"] is None


def test_report_empty_message_rejected(client: TestClient) -> None:
    assert client.post("/v1/client-errors", json={"message": ""}).status_code == 422
    assert client.post("/v1/client-errors", json={}).status_code == 422


def test_oversized_report_truncated_on_store_not_rejected(
    client: TestClient,
) -> None:
    """The stored caps truncate (a report losing its tail is fine); only the
    far larger transport caps 422. See ClientErrorReport's docstring."""
    response = client.post(
        "/v1/client-errors",
        json={"message": "M" * 9_000, "stack": "S" * 45_000},
    )
    assert response.status_code == 204
    newest = client.get("/v1/client-errors").json()["errors"][0]
    assert len(newest["message"]) == database._CLIENT_ERROR_MESSAGE_MAX_CHARS
    assert len(newest["stack"]) == database._CLIENT_ERROR_STACK_MAX_CHARS


def test_transport_cap_rejects_pathological_payload(client: TestClient) -> None:
    response = client.post(
        "/v1/client-errors", json={"message": "m", "stack": "S" * 60_000}
    )
    assert response.status_code == 422


def test_row_count_stays_bounded(client: TestClient) -> None:
    for n in range(database._CLIENT_ERRORS_MAX_ROWS + 25):
        database.record_client_error(f"err {n}", None, None, None)
    listed = client.get("/v1/client-errors", params={"limit": 200}).json()["errors"]
    # Newest survive the prune; the whole table never exceeds the cap.
    assert listed[0]["message"] == f"err {database._CLIENT_ERRORS_MAX_ROWS + 24}"
    assert len(database.list_client_errors(limit=10_000)) == (
        database._CLIENT_ERRORS_MAX_ROWS
    )


def test_list_limit_out_of_range_is_422(client: TestClient) -> None:
    """Query(ge=1, le=200) validation, matching the repo convention
    (app/routers/usage.py's days param) rather than a silent clamp."""
    assert client.get("/v1/client-errors", params={"limit": 0}).status_code == 422
    assert client.get("/v1/client-errors", params={"limit": 100_000}).status_code == 422
    assert client.get("/v1/client-errors", params={"limit": 200}).status_code == 200


def test_intake_is_public_but_review_requires_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the endpoint: a crash before login must still get
    through — while reading the stored reports stays operator-only. With a
    static token and no ADMIN_USERNAMES (the solo deployment), the token
    holder IS the operator, so GET is allowed once authenticated."""
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert (
        client.post(
            "/v1/client-errors", json={"message": "pre-login crash"}
        ).status_code
        == 204
    )
    assert client.get("/v1/client-errors").status_code == 401
    authed = client.get(
        "/v1/client-errors", headers={"Authorization": "Bearer secret-token"}
    )
    assert authed.status_code == 200
    assert authed.json()["errors"][0]["message"] == "pre-login crash"


def test_review_is_admin_gated_not_merely_authed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored stream is global and can hold another user's error text and
    `/shared/{token}` URL, so a self-registered non-admin must NOT be able to
    read it — only an admin (or the solo operator) can."""
    monkeypatch.setenv("JWT_SECRET", "client-errors-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_USERNAMES", "operator")

    def _token(username: str) -> str:
        client.post(
            "/v1/auth/register", json={"username": username, "password": "password123"}
        )
        return client.post(
            "/v1/auth/login", json={"username": username, "password": "password123"}
        ).json()["access_token"]

    mallory = _token("mallory")
    operator = _token("operator")

    assert (
        client.get(
            "/v1/client-errors", headers={"Authorization": f"Bearer {mallory}"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/v1/client-errors", headers={"Authorization": f"Bearer {operator}"}
        ).status_code
        == 200
    )


def test_intake_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /v1/client-errors sits behind the always-on auth_limiter, same
    guard login/register/`/v1/shared/{token}` get (see test_shares.py's
    equivalent). conftest force-disables it for hermeticity, so this test
    re-enables it explicitly."""
    from app import ratelimit

    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.auth_limiter, "enabled", True)
    try:
        ratelimit.auth_limiter.reset()
    except Exception:
        pass

    def _post(n: int) -> int:
        return client.post(
            "/v1/client-errors", json={"message": f"flood {n}"}
        ).status_code

    assert _post(1) == 204
    assert _post(2) == 204
    assert _post(3) == 429


def test_reporter_source_url_is_stored_as_sent(client: TestClient) -> None:
    """The backend stores source_url verbatim — share-token redaction is the
    frontend's job (see crashReporter.ts / crashReporter.test.ts), since only
    the client knows the live URL. This pins that the server doesn't mangle
    what it's given."""
    client.post(
        "/v1/client-errors",
        json={"message": "m", "source_url": "https://host/shared/<redacted>"},
    )
    newest = client.get("/v1/client-errors").json()["errors"][0]
    assert newest["source_url"] == "https://host/shared/<redacted>"
