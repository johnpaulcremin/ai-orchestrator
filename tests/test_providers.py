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


def test_call_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "call_anthropic", lambda model, q, mt, to: f"claude:{model}"
    )
    monkeypatch.setattr(orchestrator, "_call_openai", lambda *a, **k: "openai-answer")

    assert (
        orchestrator._call_model("claude-sonnet-5", "hi", 100)
        == "claude:claude-sonnet-5"
    )
    assert orchestrator._call_model("gpt-5", "hi", 100) == "openai-answer"


def test_stream_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "stream_anthropic", lambda model, q, mt, to: iter(["a", "b"])
    )
    monkeypatch.setattr(orchestrator, "_stream_openai", lambda *a, **k: iter(["x"]))

    assert list(orchestrator._stream_model("claude-x", "hi", 100)) == ["a", "b"]
    assert list(orchestrator._stream_model("gpt-5", "hi", 100)) == ["x"]


def test_run_orchestrator_answers_with_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "call_anthropic", lambda model, q, mt, to: "Bonjour"
    )

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))

    assert result.answer == "Bonjour"
    assert result.mode_used == "smart"
