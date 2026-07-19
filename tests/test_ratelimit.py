from __future__ import annotations

import pytest

from app import main, ratelimit
from app.schemas import AskResponse


def _stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main,
        "run_orchestrator",
        lambda req: AskResponse(answer="x", mode_used="fast", notes="n"),
    )


def _reset_limiter() -> None:
    try:
        ratelimit.limiter.reset()
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
