from __future__ import annotations

import pytest

from app import ratelimit
from app.routers import ask
from app.schemas import AskResponse


def _stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ask,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, **_kw: AskResponse(
            answer="x", mode_used="fast", notes="n"
        ),
    )


def _reset_limiter() -> None:
    try:
        ratelimit.limiter.reset()
    except Exception:
        pass


def _reset_auth_limiter() -> None:
    try:
        ratelimit.auth_limiter.reset()
    except Exception:
        pass


def test_ask_is_rate_limited_when_enabled(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.limiter, "enabled", True)
    _reset_limiter()
    _stub_orchestrator(monkeypatch)

    first = client.post("/v1/ask", json={"question": "a"})
    second = client.post("/v1/ask", json={"question": "b"})
    third = client.post("/v1/ask", json={"question": "c"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


def test_ask_not_limited_when_disabled(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit.limiter, "enabled", False)
    _reset_limiter()
    _stub_orchestrator(monkeypatch)

    for _ in range(5):
        assert client.post("/v1/ask", json={"question": "a"}).status_code == 200


def test_rate_limiting_enabled_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT", raising=False)
    assert ratelimit.rate_limiting_enabled() is False
    monkeypatch.setenv("RATE_LIMIT", "10/minute")
    assert ratelimit.rate_limiting_enabled() is True


def _make_request(headers: dict[str, str], peer: str = "10.0.0.1"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 12345),
    }
    return Request(scope)


def test_client_ip_uses_peer_when_proxy_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    request = _make_request({"x-forwarded-for": "1.2.3.4"}, peer="10.0.0.1")
    assert ratelimit.client_ip(request) == "10.0.0.1"


def test_client_ip_uses_forwarded_when_proxy_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    request = _make_request({"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, peer="10.0.0.1")
    assert ratelimit.client_ip(request) == "1.2.3.4"


def test_auth_endpoints_are_rate_limited(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth endpoints (register/login/logout/refresh) have their OWN
    always-on limiter, independent of RATE_LIMIT — closing the gap where they
    previously had no protection at all against brute-force/registration-spam.
    """
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.auth_limiter, "enabled", True)
    _reset_auth_limiter()

    body = {"username": "nope", "password": "wrongwrong"}
    first = client.post("/v1/auth/login", json=body)
    second = client.post("/v1/auth/login", json=body)
    third = client.post("/v1/auth/login", json=body)

    # JWT isn't enabled in this test env, so the first two are 400 (not 401) —
    # the point is that the limiter itself runs before the endpoint body, and
    # the third request is refused by the limiter regardless of that body.
    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429


def test_auth_limiter_default_value_is_five_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_RATE_LIMIT", raising=False)
    assert ratelimit.auth_rate_limit_value() == "5/minute"
    monkeypatch.setenv("AUTH_RATE_LIMIT", "3/minute")
    assert ratelimit.auth_rate_limit_value() == "3/minute"


def test_auth_is_checked_before_rate_limit(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "sekret")
    monkeypatch.setenv("RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.limiter, "enabled", True)
    _reset_limiter()
    _stub_orchestrator(monkeypatch)
    auth = {"Authorization": "Bearer sekret"}

    # Unauthenticated requests are rejected at the auth gate (401) before the
    # limiter runs, so they don't consume the budget.
    for _ in range(3):
        assert client.post("/v1/ask", json={"question": "a"}).status_code == 401

    # Authenticated traffic is still limited to 2/minute (budget not pre-spent).
    assert (
        client.post("/v1/ask", json={"question": "a"}, headers=auth).status_code == 200
    )
    assert (
        client.post("/v1/ask", json={"question": "a"}, headers=auth).status_code == 200
    )
    assert (
        client.post("/v1/ask", json={"question": "a"}, headers=auth).status_code == 429
    )


# --- refresh's per-account bucket ------------------------------------------------
#
# The per-IP bucket alone did not bound the one durable thing refresh does
# (inserting a revoked_tokens row per rotation): under TRUST_PROXY_HEADERS
# with a directly reachable backend, X-Forwarded-For is spoofable and a
# fresh per-IP bucket comes free with every request. The account key cannot
# be rotated that way — a row is only inserted for a VALID token, whose
# subject is fixed.


def _register_and_login(client, username: str = "alice") -> str:
    client.post(
        "/v1/auth/register", json={"username": username, "password": "supersecret"}
    )
    return client.post(
        "/v1/auth/login", json={"username": username, "password": "supersecret"}
    ).json()["access_token"]


def test_refresh_is_bounded_per_account_across_spoofed_ips(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attack the account bucket closes: same account, a fresh forged
    X-Forwarded-For per request. The per-IP buckets never fill; the account
    bucket does."""
    monkeypatch.setenv("JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("AUTH_RATE_LIMIT", "2/minute")
    monkeypatch.setattr(ratelimit.auth_limiter, "enabled", True)
    _reset_auth_limiter()

    token = _register_and_login(client)
    statuses = []
    for i in range(3):
        res = client.post(
            "/v1/auth/refresh",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Forwarded-For": f"203.0.113.{i}",  # a fresh spoofed IP each time
            },
        )
        statuses.append(res.status_code)
        if res.status_code == 200:
            token = res.json()["access_token"]  # keep rotating, like the attack

    assert statuses[0] == 200
    assert statuses[1] == 200
    assert statuses[2] == 429  # the account bucket, immune to the IP rotation


def test_refresh_account_keys_separate_accounts() -> None:
    """Two accounts get two buckets. Asserted at the key level, deliberately:
    end-to-end, the per-IP bucket (which SHOULD stay shared) 429s the second
    account from the same IP first, so key separation is the only per-account
    claim that can be tested without spoofing the very header this feature
    distrusts."""
    from types import SimpleNamespace

    import jwt as pyjwt

    def key_for(sub: str) -> str:
        token = pyjwt.encode({"sub": sub}, "any-key", algorithm="HS256")
        return ratelimit.refresh_account_key(
            SimpleNamespace(headers={"authorization": f"Bearer {token}"})
        )

    assert key_for("alice") == "account:alice"
    assert key_for("bob") == "account:bob"
    assert key_for("alice") != key_for("bob")


def test_refresh_account_key_prefers_the_claimed_subject() -> None:
    from types import SimpleNamespace

    import jwt as pyjwt

    token = pyjwt.encode({"sub": "alice"}, "any-key", algorithm="HS256")
    request = SimpleNamespace(headers={"authorization": f"Bearer {token}"}, client=None)
    assert ratelimit.refresh_account_key(request) == "account:alice"


def test_refresh_account_key_falls_back_to_ip_for_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No decodable claim -> the ordinary IP key, never a shared 'unknown'
    bucket that one attacker could exhaust for everyone."""
    from types import SimpleNamespace

    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    captured = {}

    def fake_client_ip(request) -> str:
        captured["called"] = True
        return "192.0.2.7"

    monkeypatch.setattr(ratelimit, "client_ip", fake_client_ip)
    request = SimpleNamespace(headers={"authorization": "Bearer not-a-jwt"})
    assert ratelimit.refresh_account_key(request) == "192.0.2.7"
    assert captured["called"] is True
