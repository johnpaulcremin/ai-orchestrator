from __future__ import annotations

import httpx
import pytest
from openai import APIError

from app import orchestrator
from app.schemas import AskRequest, Mode


def _api_error(message: str) -> APIError:
    """Build a real openai.APIError instance for use in monkeypatched calls."""
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return APIError(message, request=request, body=None)


@pytest.fixture()
def tiers(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Deterministic tier models and a stubbed client so no network is touched."""
    models = {
        "smart": "primary-smart",
        "fast": "fallback-fast",
        "base": "base-model",
    }
    monkeypatch.setenv("OPENAI_MODEL_SMART", models["smart"])
    monkeypatch.setenv("OPENAI_MODEL_FAST", models["fast"])
    monkeypatch.setenv("OPENAI_MODEL", models["base"])
    monkeypatch.delenv("OPENAI_MODEL_FALLBACK", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    return models


def test_run_orchestrator_falls_back_on_api_error(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_call(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ) -> str:
        calls.append(model)
        if model == tiers["smart"]:
            raise _api_error("primary boom")
        return f"answer from {model}"

    monkeypatch.setattr(orchestrator, "_call_openai", fake_call)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    # Primary tried first, then the fast tier as the fallback candidate.
    assert calls[0] == tiers["smart"]
    assert tiers["fast"] in calls
    assert result.answer == f"answer from {tiers['fast']}"
    assert result.mode_used.endswith("->fallback")
    assert f"fallback_model={tiers['fast']}" in result.notes


def test_run_orchestrator_returns_note_when_all_fallbacks_fail(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_fail(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ) -> str:
        raise _api_error("everything is down")

    monkeypatch.setattr(orchestrator, "_call_openai", always_fail)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert result.answer == ""
    assert "no fallback succeeded" in result.notes


def test_run_orchestrator_missing_key_returns_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_key() -> object:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Check your .env and shell env vars."
        )

    monkeypatch.setattr(orchestrator, "get_client", no_key)

    result = orchestrator.run_orchestrator(AskRequest(question="hello", mode=Mode.fast))

    assert result.answer == ""
    assert "OPENAI_API_KEY" in result.notes
    assert result.mode_used == "fast"


def test_stream_orchestrator_falls_back_before_any_delta(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ):
        if model == tiers["smart"]:
            raise _api_error("primary stream boom")
        yield "hello "
        yield "world"

    monkeypatch.setattr(orchestrator, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    names = [e["event"] for e in events]

    assert names[0] == "meta"
    assert "delta" in names
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["answer"] == "hello world"
    assert done["data"]["mode_used"].endswith("->fallback")


def test_stream_orchestrator_no_fallback_after_partial_output(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ):
        yield "partial "
        raise _api_error("died mid-stream")

    monkeypatch.setattr(orchestrator, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    names = [e["event"] for e in events]

    # A delta already went out, so no fallback is attempted — terminal error.
    assert names == ["meta", "delta", "error"]
    assert "interrupted" in events[-1]["data"]["message"].lower()


def test_stream_orchestrator_rate_limit_yields_error(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai import RateLimitError

    def fake_stream(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        raise RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        )
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="x", mode=Mode.smart))
    )
    assert [e["event"] for e in events] == ["meta", "error"]
    assert "Rate limited" in events[-1]["data"]["message"]


def test_stream_orchestrator_all_fallbacks_fail(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str, question: str, max_output_tokens: int, reasoning_effort: str = ""
    ):
        raise _api_error("everything down")
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    assert [e["event"] for e in events] == ["meta", "error"]
    assert "no fallback succeeded" in events[-1]["data"]["message"]
