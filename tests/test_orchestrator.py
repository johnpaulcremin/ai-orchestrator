from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from openai import APIError, APITimeoutError, RateLimitError

from app import orchestrator, orchestrator_calls
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
