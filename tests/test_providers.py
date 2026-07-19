from __future__ import annotations

import pytest

from app import orchestrator, providers
from app.schemas import AskRequest, Mode


def test_provider_of_classifies_by_model_name() -> None:
    assert providers.provider_of("gpt-5") == "openai"
    assert providers.provider_of("gpt-5-mini") == "openai"
    assert providers.provider_of("claude-sonnet-5") == "anthropic"
    assert providers.provider_of("CLAUDE-opus-4-8") == "anthropic"
    assert providers.provider_of("anthropic/claude-opus-4-8") == "anthropic"
    # Provider-prefixed names route through LiteLLM.
    assert providers.provider_of("gemini/gemini-2.5-pro") == "litellm"
    assert providers.provider_of("bedrock/anthropic.claude-3-5-sonnet") == "litellm"
    assert providers.provider_of("mistral/mistral-large-latest") == "litellm"
    assert providers.provider_of("groq/llama-3.3-70b") == "litellm"


def test_key_env_for_names_the_right_credential() -> None:
    assert providers.key_env_for("gpt-5") == "OPENAI_API_KEY"
    assert providers.key_env_for("claude-sonnet-5") == "ANTHROPIC_API_KEY"
    assert providers.key_env_for("gemini/gemini-2.5-pro") == "GEMINI_API_KEY"
    assert providers.key_env_for("mistral/mistral-large-latest") == "MISTRAL_API_KEY"
    assert providers.key_env_for("bedrock/anthropic.claude") == "AWS credentials"
    assert "somenew" in providers.key_env_for("somenew/model")


def test_call_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "call_anthropic", lambda model, q, mt, to: f"claude:{model}"
    )
    monkeypatch.setattr(
        orchestrator, "call_litellm", lambda model, q, mt, to, re="": f"litellm:{model}"
    )
    monkeypatch.setattr(orchestrator, "_call_openai", lambda *a, **k: "openai-answer")

    assert (
        orchestrator._call_model("claude-sonnet-5", "hi", 100)
        == "claude:claude-sonnet-5"
    )
    assert orchestrator._call_model("gpt-5", "hi", 100) == "openai-answer"
    assert (
        orchestrator._call_model("gemini/gemini-2.5-pro", "hi", 100)
        == "litellm:gemini/gemini-2.5-pro"
    )


def test_stream_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "stream_anthropic", lambda model, q, mt, to: iter(["a", "b"])
    )
    monkeypatch.setattr(
        orchestrator,
        "stream_litellm",
        lambda model, q, mt, to, re="": iter(["g1", "g2"]),
    )
    monkeypatch.setattr(orchestrator, "_stream_openai", lambda *a, **k: iter(["x"]))

    assert list(orchestrator._stream_model("claude-x", "hi", 100)) == ["a", "b"]
    assert list(orchestrator._stream_model("gpt-5", "hi", 100)) == ["x"]
    assert list(orchestrator._stream_model("mistral/large", "hi", 100)) == ["g1", "g2"]


def test_run_orchestrator_answers_with_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "call_anthropic", lambda model, q, mt, to: "Bonjour"
    )

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))

    assert result.answer == "Bonjour"
    assert result.mode_used == "smart"


def test_auth_key_env_picks_provider() -> None:
    assert orchestrator._auth_key_env("claude-sonnet-5") == "ANTHROPIC_API_KEY"
    assert orchestrator._auth_key_env("gpt-5") == "OPENAI_API_KEY"


def test_claude_auth_error_names_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from openai import AuthenticationError

    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    response = httpx.Response(401, request=httpx.Request("POST", "https://api"))

    def boom(model, q, mt, to):
        raise AuthenticationError("bad key", response=response, body=None)

    monkeypatch.setattr(orchestrator, "call_anthropic", boom)

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))
    assert result.answer == ""
    assert "ANTHROPIC_API_KEY" in result.notes


def test_non_api_error_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Claude primary that raises a plain RuntimeError (e.g. missing key at
    # client init) must still fall back to the OpenAI model — matching the
    # streaming path.
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def claude_boom(model, q, mt, to):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(orchestrator, "call_anthropic", claude_boom)
    monkeypatch.setattr(orchestrator, "_call_openai", lambda *a, **k: "recovered")

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))
    assert result.answer == "recovered"
    assert result.mode_used.endswith("->fallback")
