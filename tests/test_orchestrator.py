from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from openai import APIError, APITimeoutError, RateLimitError

from app import orchestrator, orchestrator_calls
from app.routing import RouteDecision
from app.schemas import AskRequest, AskResponse, Mode


def _api_error(message: str) -> APIError:
    """Build a real openai.APIError instance for use in monkeypatched calls."""
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return APIError(message, request=request, body=None)


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return RateLimitError(
        "slow down", response=httpx.Response(429, request=request), body=None
    )


def _timeout_error() -> APITimeoutError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return APITimeoutError(request=request)


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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        calls.append(model)
        if model == tiers["smart"]:
            raise _api_error("primary boom")
        return f"answer from {model}"

    monkeypatch.setattr(orchestrator_calls, "_call_openai", fake_call)

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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        raise _api_error("everything is down")

    monkeypatch.setattr(orchestrator_calls, "_call_openai", always_fail)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert result.answer == ""
    assert "no fallback succeeded" in result.notes
    # The raw diagnostic (notes, asserted above) is unchanged; failure_message
    # is the plain-English counterpart actually surfaced as the answer — see
    # orchestrator._provider_error_failure_message.
    assert result.failure_message == (
        "That request failed due to a provider error, not something in your "
        "question. Try regenerating — if it keeps happening, try a "
        "different model or tier."
    )


# --- Part A: plain-English failure messages ---------------------------------


def test_run_orchestrator_timeout_gets_the_timeout_specific_failure_message(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout (openai.APITimeoutError, or litellm.exceptions.Timeout for a
    LiteLLM-routed model -- see providers.TIMEOUT_ERRORS) gets a DIFFERENT
    plain-English message than any other provider error, one that mentions
    the actual elapsed time and suggests a concrete next step."""

    def always_time_out(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        raise _timeout_error()

    monkeypatch.setattr(orchestrator_calls, "_call_openai", always_time_out)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert result.answer == ""
    # notes keeps the exact same raw-diagnostic shape as any other provider
    # failure -- only failure_message differs by failure kind.
    assert "no fallback succeeded" in result.notes
    assert result.failure_message is not None
    assert result.failure_message.startswith("That request timed out after ~")
    assert "too large to complete in one pass" in result.failure_message
    assert "Try asking for one part at a time, or regenerate." in result.failure_message


def test_run_orchestrator_budget_refusal_failure_message_matches_the_refusal_text(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.0001")
    from app import database

    database.record_spend("nobody", "gpt-5", 100_000, 100_000, 1.0)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hi", mode=Mode.smart), owner="nobody"
    )

    assert result.answer == ""
    assert (
        result.failure_message
        == "Daily budget reached. Request refused; it resets at 00:00 UTC."
    )
    assert (
        result.failure_message in result.notes
    )  # notes still carries it, plus the tail


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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        if model == tiers["smart"]:
            raise _api_error("primary stream boom")
        yield "hello "
        yield "world"

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        yield "partial "
        raise _api_error("died mid-stream")

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    names = [e["event"] for e in events]

    # A delta already went out, so no fallback is attempted — terminal error.
    assert names == ["meta", "delta", "error"]
    assert "interrupted" in events[-1]["data"]["message"].lower()


def test_stream_orchestrator_client_disconnect_records_partial_spend(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropped/Stopped stream (the consumer closes the generator, as a
    disconnecting client does) must not silently lose the spend for tokens
    already billed by the provider before the disconnect.
    """

    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        for chunk in ["one ", "two ", "three "]:
            if usage is not None:
                usage.input_tokens += 5
                usage.output_tokens += 10
            yield chunk

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)
    recorded = []
    monkeypatch.setattr(
        orchestrator.database,
        "record_spend",
        lambda owner, model, in_tok, out_tok, cost: recorded.append(
            (owner, model, in_tok, out_tok)
        ),
    )

    gen = orchestrator.stream_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    next(gen)  # meta
    next(gen)  # first delta — some tokens already billed by now
    gen.close()  # simulate the client disconnecting mid-stream

    assert len(recorded) == 1
    owner, model, in_tok, out_tok = recorded[0]
    assert model == tiers["smart"]
    assert in_tok > 0
    assert out_tok > 0


def test_stream_orchestrator_client_disconnect_during_fallback_records_partial_spend(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        if model == tiers["smart"]:
            raise _api_error("primary stream boom")
        for chunk in ["fallback one ", "fallback two "]:
            if usage is not None:
                usage.input_tokens += 5
                usage.output_tokens += 10
            yield chunk

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)
    recorded = []
    monkeypatch.setattr(
        orchestrator.database,
        "record_spend",
        lambda owner, model, in_tok, out_tok, cost: recorded.append(
            (owner, model, in_tok, out_tok)
        ),
    )

    gen = orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    next(gen)  # meta
    next(gen)  # first delta from the fallback model
    gen.close()  # simulate the client disconnecting mid-fallback-stream

    assert len(recorded) == 1
    owner, model, in_tok, out_tok = recorded[0]
    assert model != tiers["smart"]
    assert in_tok > 0
    assert out_tok > 0


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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        raise RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        )
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

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
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        raise _api_error("everything down")
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    assert [e["event"] for e in events] == ["meta", "error"]
    assert "no fallback succeeded" in events[-1]["data"]["message"]
    # The raw diagnostic ("message", asserted above) is unchanged; "failure_message"
    # is the plain-English counterpart -- same split as the non-stream path.
    assert events[-1]["data"]["failure_message"] == (
        "That request failed due to a provider error, not something in your "
        "question. Try regenerating — if it keeps happening, try a "
        "different model or tier."
    )


def test_stream_orchestrator_timeout_gets_the_timeout_specific_failure_message(
    tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        raise _timeout_error()
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    assert [e["event"] for e in events] == ["meta", "error"]
    failure_message = events[-1]["data"]["failure_message"]
    assert failure_message.startswith("That request timed out after ~")
    assert "too large to complete in one pass" in failure_message


def test_stream_orchestrator_budget_refusal_failure_message_matches_the_refusal_text(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.0001")
    from app import database

    database.record_spend("nobody", "gpt-5", 100_000, 100_000, 1.0)

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question="hi", mode=Mode.smart), owner="nobody"
        )
    )
    assert [e["event"] for e in events] == ["error"]
    assert events[0]["data"]["failure_message"] == (
        "Daily budget reached. Request refused; it resets at 00:00 UTC."
    )


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


# --- fallback reason visibility (app/fallback_reason.py) --------------------


def test_run_orchestrator_success_notes_carry_the_classified_reason(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        if model == tiers["smart"]:
            raise _timeout_error()
        return f"answer from {model}"

    monkeypatch.setattr(orchestrator_calls, "_call_openai", fake_call)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart), owner="alice"
    )

    assert result.mode_used.endswith("->fallback")
    assert "fallback_reason=timeout" in result.notes

    from app import database

    counts = database.fallback_reason_counts("alice", days=1)
    assert counts == [{"reason": "timeout", "count": 1}]


def test_run_orchestrator_records_a_fallback_log_row_on_success(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        if model == tiers["smart"]:
            raise _timeout_error()
        return f"answer from {model}"

    monkeypatch.setattr(orchestrator_calls, "_call_openai", fake_call)

    orchestrator.run_orchestrator(AskRequest(question="hard problem", mode=Mode.smart))

    from app import database

    conn = sqlite3.connect(database._db_path())
    rows = conn.execute("SELECT model, reason, succeeded FROM fallback_log").fetchall()
    conn.close()
    assert rows == [(tiers["smart"], "timeout", 1)]


def test_run_orchestrator_exhausted_notes_and_log_carry_the_classified_reason(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_time_out(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        raise _timeout_error()

    monkeypatch.setattr(orchestrator_calls, "_call_openai", always_time_out)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert "fallback_reason=timeout" in result.notes

    from app import database

    assert database.fallback_reason_counts(None, days=1) == [
        {"reason": "timeout", "count": 1}
    ]


def test_run_orchestrator_reports_budget_refusal_when_every_fallback_is_blocked(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the primary fails and every fallback CANDIDATE is then refused by
    its own budget check (none ever dispatched), the operative cause of
    ending up empty-handed is the budget, not the primary's own original
    error -- see orchestrator.py's BUDGET_REFUSAL override.

    Uses REAL priced model names (gpt-5/gpt-5-mini), unlike the `tiers`
    fixture's synthetic names -- budget.reserve() never refuses an unpriced
    model (its cost can't be projected), so the daily cap can only bind here
    with models that actually have a MODEL_PRICING entry.
    """
    from app import database

    monkeypatch.setenv("OPENAI_MODEL_SMART", "gpt-5")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5-mini")
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.00")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        if model == "gpt-5":
            # Simulate the daily cap getting exhausted by other concurrent
            # traffic between the primary's own (already-passed) budget
            # check and the fallback loop's per-candidate checks.
            database.record_spend(None, "gpt-5", 1_000_000, 1_000_000, 100.0)
            raise _timeout_error()
        raise AssertionError("no fallback candidate should ever be dispatched")

    monkeypatch.setattr(orchestrator_calls, "_call_openai", fake_call)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hard problem", mode=Mode.smart)
    )

    assert result.answer == ""
    assert "fallback_reason=budget refusal" in result.notes
    assert database.fallback_reason_counts(None, days=1) == [
        {"reason": "budget_refusal", "count": 1}
    ]


def test_stream_orchestrator_success_notes_carry_the_classified_reason(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        if model == tiers["smart"]:
            raise _timeout_error()
        yield f"answer from {model}"

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    done = next(e for e in events if e["event"] == "done")
    assert "fallback_reason=timeout" in done["data"]["notes"]

    from app import database

    assert database.fallback_reason_counts(None, days=1) == [
        {"reason": "timeout", "count": 1}
    ]


def test_stream_orchestrator_exhausted_message_carries_the_classified_reason(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        raise _timeout_error()
        yield  # pragma: no cover - marks this a generator

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="hard", mode=Mode.smart))
    )
    assert "fallback_reason=timeout" in events[-1]["data"]["message"]

    from app import database

    assert database.fallback_reason_counts(None, days=1) == [
        {"reason": "timeout", "count": 1}
    ]


# --- AUTO_WORKFLOW: when the orchestrator hands off to workflow mode ----------


def _decision(**kwargs: object) -> RouteDecision:
    base = {
        "model": "gpt-5",
        "mode_used": "auto->smart",
        "notes": "n",
        "max_output_tokens": 100,
        "reasoning_effort": "medium",
        "category": "planning",
    }
    base.update(kwargs)
    return RouteDecision(**base)  # type: ignore[arg-type]


class TestShouldAutoWorkflow:
    """Every condition is a brake, not an accelerator — the single-shot path
    already handles the overwhelming majority of questions, so this only
    fires on real evidence."""

    def test_fires_when_everything_lines_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUTO_WORKFLOW", "true")
        assert orchestrator._should_auto_workflow(
            _decision(multi_part=True, deliverables=3), True, None
        )

    def test_never_fires_when_the_flag_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AUTO_WORKFLOW", raising=False)
        assert not orchestrator._should_auto_workflow(
            _decision(multi_part=True, deliverables=3), True, None
        )

    def test_never_fires_for_an_ordinary_single_artefact_question(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AUTO_WORKFLOW", "true")
        assert not orchestrator._should_auto_workflow(_decision(), True, None)

    def test_never_fires_inside_a_workflow_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # forced_category is set only for a workflow STEP. A step spawning its
        # own workflow is the recursion this guards against.
        monkeypatch.setenv("AUTO_WORKFLOW", "true")
        assert not orchestrator._should_auto_workflow(
            _decision(multi_part=True, deliverables=3), True, "coding"
        )

    def test_never_fires_on_the_workflow_fallback_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # workflow.py clears allow_auto_workflow when it falls back into the
        # orchestrator. Without this, a failed plan would bounce straight back
        # into a new workflow, forever.
        monkeypatch.setenv("AUTO_WORKFLOW", "true")
        assert not orchestrator._should_auto_workflow(
            _decision(multi_part=True, deliverables=3), False, None
        )


def test_run_orchestrator_hands_a_multi_artefact_request_to_workflow_mode(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_WORKFLOW", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: _decision(multi_part=True, deliverables=3),
    )

    seen: dict[str, object] = {}

    def fake_run_workflow(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.update(kwargs)
        return AskResponse(
            answer="workflow answer", mode_used="auto->workflow(3 steps)", notes="n"
        )

    import app.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "run_workflow", fake_run_workflow)

    result = orchestrator.run_orchestrator(
        AskRequest(question="summary, spreadsheet and chart", mode=Mode.auto)
    )

    assert result.answer == "workflow answer"
    assert result.mode_used == "auto->workflow(3 steps)"
    assert seen["auto_routed"] is True
    # The classification is handed down so the fallback can reuse it rather
    # than paying for a second classifier call.
    assert seen["fallback_category"] == "planning"


def test_run_orchestrator_leaves_an_ordinary_question_on_the_single_shot_path(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_WORKFLOW", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: _decision())

    import app.workflow as workflow_module

    routed_to_workflow: list[bool] = []

    def record(*a: object, **k: object) -> AskResponse:
        routed_to_workflow.append(True)
        return AskResponse(answer="", mode_used="workflow", notes="")

    monkeypatch.setattr(workflow_module, "run_workflow", record)

    # Whatever the downstream model call does (there is no real client here),
    # the assertion is that the request never left the single-shot path.
    result = orchestrator.run_orchestrator(
        AskRequest(question="compare A and B", mode=Mode.auto)
    )
    assert routed_to_workflow == []
    assert "workflow" not in result.mode_used


# --- artefact steps must land on a code-execution-capable model ---------------


class TestCodeExecutionOverride:
    """orchestrator._apply_code_execution_override — the piece that did not
    exist: nothing could ask for code execution per step, so a step routed by
    its category to a Gemini/Ollama model silently lost the tool and wrote a
    markdown table instead of producing the file it was asked for."""

    def test_moves_an_artefact_step_off_a_model_that_cannot_run_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODE_EXECUTION", "true")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
        monkeypatch.setenv("OPENAI_MODEL_SMART", "gpt-5")
        decision = _decision(model="gemini/gemini-flash-latest")

        moved = orchestrator._apply_code_execution_override(decision, True)

        assert moved.model == "gpt-5"
        assert "code execution" in moved.notes

    def test_keeps_a_capable_model_but_still_raises_its_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The model needs no change — but a capable model on a text-sized
        ceiling is exactly what truncated the observed spreadsheet, so the
        budget is raised whether or not the model moved."""
        monkeypatch.setenv("CODE_EXECUTION", "true")
        monkeypatch.setenv("ARTEFACT_MAX_OUTPUT_TOKENS", "8000")
        decision = _decision(model="gpt-5")

        result = orchestrator._apply_code_execution_override(decision, True)

        assert result.model == "gpt-5"
        assert result.max_output_tokens == 8000
        assert "ceiling raised" in result.notes

    def test_leaves_a_step_that_already_has_the_headroom_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only ever RAISES: a step whose tier already allows more than the
        artefact figure keeps its own budget, and nothing is rewritten."""
        monkeypatch.setenv("CODE_EXECUTION", "true")
        monkeypatch.setenv("ARTEFACT_MAX_OUTPUT_TOKENS", "4000")
        decision = _decision(model="gpt-5", max_output_tokens=10000)

        assert orchestrator._apply_code_execution_override(decision, True) is decision

    def test_never_touches_a_prose_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Per-step category routing works and must stay exactly as it is.
        monkeypatch.setenv("CODE_EXECUTION", "true")
        decision = _decision(model="gemini/gemini-flash-latest")
        assert orchestrator._apply_code_execution_override(decision, False) is decision

    def test_degrades_to_text_when_code_execution_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Item 6: with the feature off there is no file to produce, so the
        step stays where its category put it and answers in text. Never an
        error, never a pointless model upgrade."""
        monkeypatch.setenv("CODE_EXECUTION", "false")
        decision = _decision(model="gemini/gemini-flash-latest")
        assert orchestrator._apply_code_execution_override(decision, True) is decision

    def test_degrades_when_nothing_configured_can_run_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODE_EXECUTION", "true")
        monkeypatch.setenv("OPENAI_MODEL", "ollama/llama3")
        monkeypatch.setenv("OPENAI_MODEL_SMART", "gemini/gemini-flash-latest")
        monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3")
        decision = _decision(model="gemini/gemini-flash-latest")

        assert orchestrator._apply_code_execution_override(decision, True) is decision


def test_code_execution_capable_model_is_the_single_source_of_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the routing override and workflow's reservation pricing ask this
    one function, so the reservation can never quote a model the workflow
    will not actually use."""
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gemini/gemini-flash-latest")

    assert orchestrator.code_execution_capable_model("gpt-5") == "gpt-5"
    assert orchestrator.code_execution_capable_model("gemini/gemini-flash-latest") == (
        "gpt-5"
    )


def test_code_execution_capable_model_returns_none_when_nothing_qualifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "ollama/llama3")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "ollama/llama3")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "ollama/llama3")
    assert orchestrator.code_execution_capable_model("ollama/llama3") is None


# --- code execution on the fallback path --------------------------------------


def _capturing_call(
    fail_on: str, seen: list[tuple[str, object]], make_file: bool = False
):
    """A _call_openai stand-in that fails one model and records the
    (model, code_execution) pair every other call was dispatched with."""

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        seen.append((model, code_execution))
        if model == fail_on:
            raise _timeout_error()
        if make_file and code_execution and code_results is not None:
            code_results.append(
                {
                    "code": "df.to_csv('out.csv')",
                    "logs": "ok",
                    "images": [],
                    "files": [
                        {
                            "filename": "out.csv",
                            "mime_type": "text/csv",
                            "data": "data:text/csv;base64,YSxiCg==",
                        }
                    ],
                }
            )
        return f"answer from {model}"

    return fake_call


def test_the_fallback_gets_code_execution_derived_from_its_own_model(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap: a request whose whole point is a FILE used to fail over to a
    tool-less retry, so the deliverable was lost even when the replacement
    model could perfectly well have built it.

    Deliberately NOT inherited from the primary — every other hosted tool
    stays off on fallback (a documented scope limit), and this one is
    re-derived for the model actually being called."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _capturing_call(tiers["smart"], seen)
    )

    orchestrator.run_orchestrator(AskRequest(question="build a sheet", mode=Mode.smart))

    # Both are bare names, so both are OpenAI-served and both get the tool.
    assert seen == [(tiers["smart"], True), (tiers["fast"], True)]


def test_a_fallback_onto_a_litellm_model_still_gets_no_code_execution(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-derived means re-derived: a fallback that lands on a provider with
    no hosted tools must not be handed a flag it cannot honour."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-flash-latest")
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _capturing_call(tiers["smart"], seen)
    )
    monkeypatch.setattr(
        orchestrator_calls, "call_litellm", lambda *a, **k: "answer from gemini"
    )

    orchestrator.run_orchestrator(AskRequest(question="build a sheet", mode=Mode.smart))

    assert seen[0] == (tiers["smart"], True)
    assert all(model != "gemini/gemini-flash-latest" for model, _ in seen[1:])


def test_a_file_the_fallback_produced_reaches_the_answer_and_is_never_cached(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabling the tool is only half of it — the results have to be
    collected onto the response, and a code_results payload has no cache
    column, so an answer carrying one must not be frozen into the cache (the
    same rule the primary path already follows)."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls,
        "_call_openai",
        _capturing_call(tiers["smart"], seen, make_file=True),
    )
    put_calls: list[object] = []
    monkeypatch.setattr(orchestrator.cache, "put", lambda *a, **k: put_calls.append(a))

    result = orchestrator.run_orchestrator(
        AskRequest(question="build a sheet", mode=Mode.smart)
    )

    assert result.code_results is not None
    files = [f for cr in result.code_results for f in (cr.files or [])]
    assert [f.filename for f in files] == ["out.csv"]
    assert result.mode_used.endswith("->fallback")
    assert put_calls == [], "an answer carrying executed code was cached"


# --- every hosted tool on the fallback path ------------------------------------


# _call_openai is dispatched POSITIONALLY, so a stand-in must declare the whole
# signature rather than taking **kwargs — same shape as the helpers above.
def _recording_call(fail_on: str, seen: list[dict[str, object]]):
    """Fails one model and records the full tool arguments of every other
    call, so a test can assert what the FALLBACK was dispatched with."""

    def fake_call(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ) -> str:
        seen.append(
            {
                "model": model,
                "web_search": web_search,
                "actions": actions,
                "images": images,
                "code_execution": code_execution,
                "math_solve": math_solve,
                "capabilities": capabilities,
            }
        )
        if model == fail_on:
            raise _timeout_error()
        if web_search and citations is not None:
            citations.append(  # type: ignore[union-attr]
                {"title": "A source", "url": "https://example.com/a"}
            )
        if actions and pending_action is not None:
            pending_action.append(  # type: ignore[union-attr]
                {"action": "send_email", "summary": "s", "payload": {}}
            )
        if capabilities and capabilities_calls is not None:
            capabilities_calls.append(True)  # type: ignore[union-attr]
        return f"answer from {model}"

    return fake_call


def test_the_fallback_gets_web_search_and_its_citations_reach_the_answer(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshness question whose primary failed used to come back ungrounded:
    the fallback was dispatched with no tools at all, so it answered from
    training data and the sources list was empty, with nothing saying so."""
    decision = _decision(model=tiers["smart"], needs_live_data=True)
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _recording_call(tiers["smart"], seen)
    )

    result = orchestrator.run_orchestrator(
        AskRequest(question="what happened today", mode=Mode.auto)
    )

    assert result.mode_used.endswith("->fallback")
    assert seen[-1]["web_search"] is True
    assert result.sources is not None
    assert [s.url for s in result.sources] == ["https://example.com/a"]


def test_the_fallback_derives_its_tools_from_its_own_model(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-derived means re-derived. A LiteLLM-served fallback has no hosted
    tools wired up at all, so it must not be handed flags it cannot honour —
    the mirror image of the OpenAI fallback above."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-flash-latest")
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _recording_call(tiers["smart"], seen)
    )
    litellm_models: list[str] = []
    monkeypatch.setattr(
        orchestrator_calls,
        "call_litellm",
        lambda model, *a, **k: litellm_models.append(model) or "answer from gemini",
    )

    orchestrator.run_orchestrator(AskRequest(question="hello", mode=Mode.smart))

    # The OpenAI primary was offered code execution...
    assert seen[0]["code_execution"] is True
    # ...and the Gemini fallback was reached without any of it: call_litellm
    # takes no tool arguments at all, which is the point — _tool_flags_for
    # refused them for that provider before dispatch.
    assert litellm_models == ["gemini/gemini-flash-latest"]


def test_a_fact_check_lookup_still_runs_when_the_primary_failed(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """fact_check and academic_search are standalone HTTP lookups that never
    touch the model. They were skipped on fallback purely by sharing the code
    path — nothing about a failed model call makes a published fact-check
    less available."""
    monkeypatch.setenv("FACT_CHECK", "true")
    monkeypatch.setattr(orchestrator, "looks_like_fact_check_request", lambda _q: True)
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda _q: [{"claim": "c", "rating": "False", "publisher": "p", "url": "u"}],
    )
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _recording_call(tiers["smart"], seen)
    )

    result = orchestrator.run_orchestrator(
        AskRequest(question="is this claim true", mode=Mode.smart)
    )

    assert result.mode_used.endswith("->fallback")
    assert result.fact_checks is not None
    assert [f.claim for f in result.fact_checks] == ["c"]


def test_a_fallback_answer_carrying_a_tool_result_is_never_cached(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cacheable check reads THIS call's collectors. Before, it consulted
    the PRIMARY's pending_action/generated_images lists — belonging to a call
    that had already failed — so a fallback answer carrying a tool result
    could be frozen into the cache and replayed without it."""
    # Actions are enabled by configuring a webhook, not by a flag (see
    # actions.actions_enabled).
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://example.com/hook")
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _recording_call(tiers["smart"], seen)
    )
    put_calls: list[object] = []
    monkeypatch.setattr(orchestrator.cache, "put", lambda *a, **k: put_calls.append(a))

    result = orchestrator.run_orchestrator(
        AskRequest(question="email bob", mode=Mode.smart)
    )

    assert seen[-1]["actions"] is True
    assert result.pending_action is not None
    assert put_calls == [], "a fallback answer with a pending action was cached"


def test_the_streaming_fallback_streams_its_notes_and_carries_its_sources(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming path builds its own failover block, and its notes have to
    reach the reader as DELTAS rather than only appearing in the persisted
    text — the same shape the streaming primary uses. Its own test, because
    the two loops are separate copies."""
    monkeypatch.setenv("FACT_CHECK", "true")
    monkeypatch.setattr(orchestrator, "looks_like_fact_check_request", lambda _q: True)
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda _q: [{"claim": "c", "rating": "False", "publisher": "p", "url": "u"}],
    )

    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        if model == tiers["smart"]:
            raise _timeout_error()
        if citations is not None:
            citations.append({"title": "A source", "url": "https://example.com/a"})
        yield "answer from the fallback"

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question="is this claim true", mode=Mode.smart)
        )
    )

    deltas = [e["data"]["text"] for e in events if e["event"] == "delta"]
    assert any(
        "fact-check" in d.lower() or "fact check" in d.lower() for d in deltas
    ), f"the fact-check note never reached the reader as a delta: {deltas}"
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["fact_checks"]


def test_a_capabilities_answer_from_the_fallback_is_never_remembered(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capabilities snapshot carries live per-owner account state —
    remaining daily budget, free-lane quotas, the effective model map. The
    primary path has always refused to remember it; the fallback could not
    produce one until it was given the tool, so `memorable` defaulting to True
    was harmless there and is not any more."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    seen: list[dict[str, object]] = []

    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", _recording_call(tiers["smart"], seen)
    )
    # Force the capabilities branch: the fallback model "called" the tool.
    monkeypatch.setattr(
        orchestrator, "capabilities_snapshot", lambda _owner: {"models": {}}
    )
    monkeypatch.setattr(orchestrator, "self_describe_note", lambda _snap: "CAPS NOTE")
    monkeypatch.setattr(
        orchestrator, "looks_like_capabilities_request", lambda _q: True
    )

    result = orchestrator.run_orchestrator(
        AskRequest(question="what can you do", mode=Mode.smart), owner="johnpaul"
    )

    assert result.mode_used.endswith("->fallback")
    assert "CAPS NOTE" in result.answer
    assert result.memorable is False


def test_the_streaming_fallback_marks_a_capabilities_answer_unrememberable(
    db_path: Path, tiers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming twin: `memorable` rides the done event as a private key,
    and its ABSENCE means rememberable (see _shared.py's bool(pop(...,
    True))), so it has to be emitted rather than left out."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(
        orchestrator, "capabilities_snapshot", lambda _owner: {"models": {}}
    )
    monkeypatch.setattr(orchestrator, "self_describe_note", lambda _snap: "CAPS NOTE")
    monkeypatch.setattr(
        orchestrator, "looks_like_capabilities_request", lambda _q: True
    )

    def fake_stream(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage=None,
        web_search: bool = False,
        citations: object = None,
        search_queries: object = None,
        actions: bool = False,
        pending_action: object = None,
        images: bool = False,
        generated_images: object = None,
        attachments: object = None,
        files: object = None,
        truncated: object = None,
        code_execution: object = None,
        code_results: object = None,
        math_solve: object = None,
        math_results: object = None,
        capabilities: object = None,
        capabilities_calls: object = None,
        cacheable_system: object = None,
        anthropic_question: object = None,
    ):
        if model == tiers["smart"]:
            raise _timeout_error()
        if capabilities and capabilities_calls is not None:
            capabilities_calls.append(True)  # type: ignore[union-attr]
        yield "answer from the fallback"

    monkeypatch.setattr(orchestrator_calls, "_stream_openai", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question="what can you do", mode=Mode.smart), owner="johnpaul"
        )
    )

    done = next(e for e in events if e["event"] == "done")
    assert done["data"].get("memorable") is False
