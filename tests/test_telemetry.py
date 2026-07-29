from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from app import orchestrator
from app.schemas import AskRequest, Mode
from app.telemetry import StageTimer, elapsed_ms, new_request_meta


def test_new_request_meta_has_id_and_start_time() -> None:
    meta = new_request_meta()
    assert meta.request_id
    assert meta.started_ms > 0


def test_elapsed_ms_grows_over_time() -> None:
    meta = new_request_meta()
    time.sleep(0.01)
    assert elapsed_ms(meta) >= 10


def test_stage_timer_mark_records_a_delta_from_the_previous_mark() -> None:
    meta = new_request_meta()
    timer = StageTimer(meta)
    time.sleep(0.01)
    timer.mark("a")
    time.sleep(0.01)
    timer.mark("b")

    stages = timer.stages()
    assert [name for name, _ in stages] == ["a", "b"]
    # Each stage's OWN duration, not a running cumulative total.
    assert all(duration >= 5 for _, duration in stages)


def test_stage_timer_record_uses_the_supplied_duration_as_is() -> None:
    meta = new_request_meta()
    timer = StageTimer(meta)
    timer.record("memory_embed", 42)
    assert timer.stages() == [("memory_embed", 42)]


def test_stage_timer_combines_recorded_and_marked_stages_in_order() -> None:
    meta = new_request_meta()
    timer = StageTimer(meta)
    timer.record("memory_embed", 42)
    timer.mark("routing")
    timer.mark("model_call")

    names = [name for name, _ in timer.stages()]
    assert names == ["memory_embed", "routing", "model_call"]


def test_stage_timer_summary_is_a_compact_string() -> None:
    meta = new_request_meta()
    timer = StageTimer(meta)
    timer.record("memory_embed", 10)
    timer.mark("routing")
    summary = timer.summary()
    assert "memory_embed=10ms" in summary
    assert "routing=" in summary


def test_stage_timer_with_no_stages_has_an_empty_summary() -> None:
    meta = new_request_meta()
    timer = StageTimer(meta)
    assert timer.stages() == []
    assert timer.summary() == ""


# --- integration: orchestrator.py actually logs a per-stage breakdown -------------


def _stub_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call_model(**kwargs: object) -> str:
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 5  # type: ignore[attr-defined]
            usage.output_tokens = 7  # type: ignore[attr-defined]
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)


def _stub_stream_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_stream_model(**kwargs: object):
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 5  # type: ignore[attr-defined]
            usage.output_tokens = 7  # type: ignore[attr-defined]
        yield "the answer"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)


def test_run_orchestrator_logs_a_per_stage_breakdown(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    _stub_call_model(monkeypatch)

    with caplog.at_level(logging.INFO):
        result = orchestrator.run_orchestrator(
            AskRequest(question="what is 2+2", mode=Mode.fast)
        )

    assert result.answer == "the answer"
    ok_lines = [line for line in caplog.text.splitlines() if "request.ok" in line]
    assert len(ok_lines) == 1
    line = ok_lines[0]
    assert "stages=[" in line
    for stage in (
        "cache=",
        "semantic_cache=",
        "routing=",
        "moderation=",
        "budget=",
        "model_call=",
        "post_processing=",
    ):
        assert stage in line, f"missing {stage!r} in: {line}"


def test_run_orchestrator_folds_pre_stage_timings_into_the_breakdown(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    _stub_call_model(monkeypatch)

    with caplog.at_level(logging.INFO):
        orchestrator.run_orchestrator(
            AskRequest(question="what is 2+2", mode=Mode.fast),
            pre_stage_timings={"memory_embed": 37},
        )

    ok_lines = [line for line in caplog.text.splitlines() if "request.ok" in line]
    assert "memory_embed=37ms" in ok_lines[0]
    # An externally-recorded stage must come before the ones this function
    # marks itself -- it genuinely happened first, before this call even started.
    assert ok_lines[0].index("memory_embed=37ms") < ok_lines[0].index("cache=")


def test_stream_orchestrator_logs_a_per_stage_breakdown(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    _stub_stream_model(monkeypatch)

    with caplog.at_level(logging.INFO):
        events = list(
            orchestrator.stream_orchestrator(
                AskRequest(question="what is 2+2", mode=Mode.fast)
            )
        )

    assert any(e["event"] == "done" for e in events)
    ok_lines = [line for line in caplog.text.splitlines() if "stream.ok" in line]
    assert len(ok_lines) == 1
    line = ok_lines[0]
    assert "stages=[" in line
    for stage in (
        "cache=",
        "semantic_cache=",
        "routing=",
        "moderation=",
        "budget=",
        "model_call=",
        "post_processing=",
    ):
        assert stage in line, f"missing {stage!r} in: {line}"
