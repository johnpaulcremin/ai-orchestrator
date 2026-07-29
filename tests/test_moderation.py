"""Moderation: the opt-in independent safety-net check on the incoming
question, run before any budget reservation or model call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.orchestrator as orchestrator
from app import moderation
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode


# --- moderation.py: config + check_question + refusal_note ------------------


def test_moderation_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODERATION", raising=False)
    assert moderation.moderation_enabled() is False
    monkeypatch.setenv("MODERATION", "true")
    assert moderation.moderation_enabled() is True
    monkeypatch.setenv("MODERATION", "false")
    assert moderation.moderation_enabled() is False


def _fake_categories(**flags: bool) -> object:
    """A fresh one-off type per call, so `type(instance).model_fields`
    (what check_question actually reads, mirroring pydantic's class-level
    model_fields) reflects exactly this call's category set — without
    mutating any shared class, real or fake."""
    cls = type("FakeCategories", (), {"model_fields": dict.fromkeys(flags), **flags})
    return cls()


def _fake_moderation_client(flagged: bool, categories: dict[str, bool]):
    result = SimpleNamespace(flagged=flagged, categories=_fake_categories(**categories))
    create = lambda **_kw: SimpleNamespace(results=[result])  # noqa: E731
    return SimpleNamespace(moderations=SimpleNamespace(create=create))


def test_check_question_clean_returns_empty() -> None:
    client = _fake_moderation_client(False, {"violence": False, "hate": False})
    assert moderation.check_question(client, "how do I bake bread?") == []


def test_check_question_flagged_returns_category_names() -> None:
    client = _fake_moderation_client(
        True, {"violence": True, "hate": False, "harassment": True}
    )
    flagged = moderation.check_question(client, "bad stuff")
    assert set(flagged) == {"violence", "harassment"}


def test_check_question_empty_input_skips_api_call() -> None:
    def create(**_kw):
        raise AssertionError("must not call the API for empty input")

    client = SimpleNamespace(moderations=SimpleNamespace(create=create))
    assert moderation.check_question(client, "   ") == []


def test_check_question_tolerates_api_failure() -> None:
    def create(**_kw):
        raise RuntimeError("boom")

    client = SimpleNamespace(moderations=SimpleNamespace(create=create))
    assert moderation.check_question(client, "hello") == []


def test_check_question_passes_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERATION_MODEL", "custom-moderation-model")
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            results=[SimpleNamespace(flagged=False, categories=_fake_categories())]
        )

    client = SimpleNamespace(moderations=SimpleNamespace(create=create))
    moderation.check_question(client, "hello")
    assert captured["model"] == "custom-moderation-model"


def test_refusal_note_lists_sorted_categories() -> None:
    note = moderation.refusal_note(["violence", "hate"])
    assert "hate, violence" in note


# --- orchestrator: gating ----------------------------------------------------


def test_run_orchestrator_disabled_by_default_skips_moderation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fail_check(*_a, **_k):
        raise AssertionError("must not check moderation when disabled")

    monkeypatch.setattr(orchestrator, "check_question", fail_check)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.answer == "ok"


def test_run_orchestrator_clean_question_proceeds_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "check_question", lambda *_a, **_k: [])
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.answer == "ok"


def test_run_orchestrator_flagged_question_refuses_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "check_question", lambda *_a, **_k: ["violence"])

    def fail_call_model(**_kw):
        raise AssertionError("must not call the model when moderation flags")

    monkeypatch.setattr(orchestrator, "_call_model", fail_call_model)

    result = run_orchestrator(AskRequest(question="bad stuff", mode=Mode.smart))
    assert result.answer == ""
    assert "moderation" in result.notes
    assert "violence" in result.notes


def test_stream_orchestrator_disabled_by_default_skips_moderation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fail_check(*_a, **_k):
        raise AssertionError("must not check moderation when disabled")

    monkeypatch.setattr(orchestrator, "check_question", fail_check)
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    assert events[-1]["event"] == "done"


def test_stream_orchestrator_flagged_question_yields_error_no_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "check_question", lambda *_a, **_k: ["hate"])

    def fail_stream_model(**_kw):
        raise AssertionError("must not call the model when moderation flags")

    monkeypatch.setattr(orchestrator, "_stream_model", fail_stream_model)

    events = list(
        stream_orchestrator(AskRequest(question="bad stuff", mode=Mode.smart))
    )
    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert "hate" in events[0]["data"]["message"]
