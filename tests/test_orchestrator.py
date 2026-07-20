from __future__ import annotations

import httpx
import pytest
from openai import APIError, RateLimitError

from app import orchestrator
from app.schemas import AskRequest, Mode


def _api_error(message: str) -> APIError:
    """Build a real openai.APIError instance for use in monkeypatched calls."""
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return APIError(message, request=request, body=None)


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
    ) -> str:
        raise _api_error("everything is down")

    monkeypatch.setattr(orchestrator, "_call_openai", always_fail)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert result.answer == ""
    assert "no fallback succeeded" in result.notes


# --- cross-vendor fallback on rate-limit errors -----------------------------


def test_fallback_models_prefers_cross_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "claude-sonnet-5")

    # Primary is OpenAI, so the Claude fallback (a different provider) is first.
    fb = orchestrator._fallback_models("gpt-5")
    assert fb[0] == "claude-sonnet-5"
    assert "gpt-5-mini" in fb  # same-provider candidate kept, but after

    # cross_provider_only drops same-provider entirely (rate-limit failover).
    assert orchestrator._fallback_models("gpt-5", cross_provider_only=True) == [
        "claude-sonnet-5"
    ]


def test_rate_limit_fails_over_to_cross_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gpt-primary")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    calls: list[str] = []

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
    ) -> str:
        calls.append(model)
        if orchestrator.provider_of(model) == "openai":
            raise _rate_limit_error()  # the throttled key
        return f"answer from {model}"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call)

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))

    assert calls[0] == "gpt-primary"
    assert "claude-sonnet-5" in calls  # failed over to the other vendor
    assert result.answer == "answer from claude-sonnet-5"
    assert result.mode_used.endswith("->fallback")


def test_rate_limit_without_cross_vendor_does_not_hammer_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gpt-primary")
    monkeypatch.delenv("OPENAI_MODEL_FALLBACK", raising=False)  # only OpenAI models
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    calls: list[str] = []

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
    ) -> str:
        calls.append(model)
        raise _rate_limit_error()

    monkeypatch.setattr(orchestrator, "_call_model", fake_call)

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))

    assert result.answer == ""
    assert "Rate limited" in result.notes
    # No same-vendor fallback is tried — the throttled key is hit exactly once.
    assert calls == ["gpt-primary"]


def test_stream_rate_limit_fails_over_to_cross_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gpt-primary")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
    ):
        if orchestrator.provider_of(model) == "openai":
            raise _rate_limit_error()
        yield "hi from "
        yield model

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="x", mode=Mode.smart))
    )

    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["answer"] == "hi from claude-sonnet-5"
    assert done["data"]["mode_used"].endswith("->fallback")


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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
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
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
    ):
        raise _api_error("everything down")
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    assert [e["event"] for e in events] == ["meta", "error"]
    assert "no fallback succeeded" in events[-1]["data"]["message"]


# --- review follow-up: LiteLLM vendor granularity for cross-vendor failover ---


def test_vendor_of_distinguishes_litellm_providers() -> None:
    assert orchestrator._vendor_of("gemini/gemini-2.5-pro") == "gemini"
    assert orchestrator._vendor_of("mistral/mistral-large") == "mistral"
    assert orchestrator._vendor_of("gpt-5") == "openai"
    assert orchestrator._vendor_of("claude-sonnet-5") == "anthropic"


def test_fallback_models_treats_litellm_vendors_as_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "mistral/mistral-large")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-2.5-flash")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-2.5-pro")
    # Primary is a Gemini model; Mistral is a genuinely different LiteLLM vendor.
    fb = orchestrator._fallback_models(
        "gemini/gemini-2.5-pro", cross_provider_only=True
    )
    assert "mistral/mistral-large" in fb  # cross-vendor failover works
    assert "gemini/gemini-2.5-flash" not in fb  # same vendor dropped in cross-only
