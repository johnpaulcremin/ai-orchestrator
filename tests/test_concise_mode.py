"""Concise-mode (CONCISE_MODE): an opt-in brevity instruction appended to the
outgoing prompt, since output tokens typically bill far more than input
tokens. Off by default — unlike the automatic image-cost transforms, this
changes what the model actually says, so it needs an explicit opt-in.
"""

from __future__ import annotations

import pytest

import app.orchestrator as orchestrator
from app.orchestrator import _CONCISE_INSTRUCTION, apply_concise_mode
from app.schemas import AskRequest, Mode


# --- apply_concise_mode: unit behavior -----------------------------------------


def test_apply_concise_mode_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_concise_mode_enabled", lambda: False)
    question, cacheable_system = apply_concise_mode("hi", "SYSTEM")
    assert question == "hi"
    assert cacheable_system == "SYSTEM"


def test_apply_concise_mode_appends_to_question_when_no_cacheable_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_concise_mode_enabled", lambda: True)
    question, cacheable_system = apply_concise_mode("hi", None)
    assert question == f"hi\n\n{_CONCISE_INSTRUCTION}"
    assert cacheable_system is None


def test_apply_concise_mode_appends_to_both_when_cacheable_system_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_concise_mode_enabled", lambda: True)
    question, cacheable_system = apply_concise_mode("hi", "SYSTEM")
    assert question == f"hi\n\n{_CONCISE_INSTRUCTION}"
    assert cacheable_system == f"SYSTEM\n\n{_CONCISE_INSTRUCTION}"


# --- orchestrator wiring: run_orchestrator / stream_orchestrator ---------------


def test_run_orchestrator_sends_concise_instruction_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCISE_MODE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="what is this", mode=Mode.smart))
    assert _CONCISE_INSTRUCTION in seen["kwargs"]["question"]


def test_run_orchestrator_omits_concise_instruction_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="what is this", mode=Mode.smart))
    assert _CONCISE_INSTRUCTION not in seen["kwargs"]["question"]


def test_run_orchestrator_appends_concise_instruction_to_cacheable_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCISE_MODE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(
        AskRequest(question="what is this", mode=Mode.smart),
        cacheable_system="STABLE SYSTEM BLOCK",
        anthropic_question="JUST THE NEW TURN",
    )
    assert seen["kwargs"]["cacheable_system"] == (
        f"STABLE SYSTEM BLOCK\n\n{_CONCISE_INSTRUCTION}"
    )


def test_run_orchestrator_fallback_also_gets_concise_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from openai import APIError

    monkeypatch.setenv("CONCISE_MODE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_fallback_models", lambda *a, **k: ["fallback-model"]
    )

    seen = {}

    def fake_call_model(**kwargs):
        if kwargs["model"] != "fallback-model":
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise APIError("boom", request=request, body=None)
        seen["kwargs"] = kwargs
        return "recovered"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(
        AskRequest(question="what is this", mode=Mode.smart)
    )
    assert result.answer == "recovered"
    assert _CONCISE_INSTRUCTION in seen["kwargs"]["question"]


def test_stream_orchestrator_sends_concise_instruction_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCISE_MODE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_stream_model(**kwargs):
        seen["kwargs"] = kwargs
        yield "ok"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    list(
        orchestrator.stream_orchestrator(
            AskRequest(question="what is this", mode=Mode.smart)
        )
    )
    assert _CONCISE_INSTRUCTION in seen["kwargs"]["question"]
