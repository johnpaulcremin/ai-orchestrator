"""Avoided-cost logging: when the app's own response cache serves an answer
instead of calling a model, that avoided spend is now durably logged (a
separate table from spend_log — see database.avoided_cost_log's schema
comment for why) instead of silently vanishing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.orchestrator as orchestrator
from app.database import avoided_cost_today
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode


def _stub_priced_model(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_call_model(**kwargs: object) -> str:
        calls.append(str(kwargs["model"]))
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 1_000_000  # type: ignore[attr-defined]
            usage.output_tokens = 0  # type: ignore[attr-defined]
        return "answer-42"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)


def _stub_priced_stream_model(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    def fake_stream_model(**kwargs: object):
        calls.append(str(kwargs["model"]))
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 1_000_000  # type: ignore[attr-defined]
            usage.output_tokens = 0  # type: ignore[attr-defined]
        yield "answer-42"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)


def test_cache_hit_logs_the_avoided_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-nano")  # priced: (0.05, 0.40, 0.005)
    calls: list[str] = []
    _stub_priced_model(monkeypatch, calls)

    assert avoided_cost_today(None) == 0.0

    first = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert first.cached is False
    assert first.cost_usd == pytest.approx(0.05)  # 1M input tokens @ $0.05/1M

    # Not yet logged — the FIRST call was a real spend, not an avoided one.
    assert avoided_cost_today(None) == 0.0

    second = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert second.cached is True
    assert len(calls) == 1  # the model was not called again

    # The second call's avoided cost equals what the first call actually cost.
    assert avoided_cost_today(None) == pytest.approx(0.05)


def test_cache_hit_avoided_cost_is_scoped_to_owner(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-nano")
    calls: list[str] = []
    _stub_priced_model(monkeypatch, calls)

    run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast), owner="alice")
    run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast), owner="alice")

    assert avoided_cost_today("alice") == pytest.approx(0.05)
    assert avoided_cost_today("bob") == 0.0
    assert avoided_cost_today(None) == 0.0


def test_streaming_cache_hit_logs_the_avoided_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-nano")
    calls: list[str] = []
    _stub_priced_stream_model(monkeypatch, calls)

    list(stream_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast)))
    assert avoided_cost_today(None) == 0.0  # first call: real spend, not avoided

    list(stream_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast)))
    assert len(calls) == 1
    assert avoided_cost_today(None) == pytest.approx(0.05)


def test_cache_hit_for_an_unpriced_model_logs_a_null_avoided_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "totally-unpriced-model")
    calls: list[str] = []
    _stub_priced_model(monkeypatch, calls)

    first = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert first.cost_usd is None

    second = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert second.cached is True

    # COALESCE makes the aggregate read as $0 for an unpriced call — never
    # conflated with "the cache genuinely avoided zero cost" — but the
    # underlying row must still exist, recording that an avoided call
    # happened at all, with a NULL (unknown) rather than a false $0 amount.
    assert avoided_cost_today(None) == 0.0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT model, reason, avoided_cost_usd FROM avoided_cost_log"
        ).fetchone()
    assert row == ("totally-unpriced-model", "response_cache_hit", None)


def test_no_cache_hit_means_no_avoided_cost_logged(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gpt-5-nano")
    calls: list[str] = []
    _stub_priced_model(monkeypatch, calls)

    run_orchestrator(AskRequest(question="q1", mode=Mode.fast))
    run_orchestrator(
        AskRequest(question="q2", mode=Mode.fast)
    )  # different question: no hit

    assert len(calls) == 2
    assert avoided_cost_today(None) == 0.0
