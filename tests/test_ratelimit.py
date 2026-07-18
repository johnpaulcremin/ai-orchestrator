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
