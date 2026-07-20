"""Global daily spend cap (DAILY_BUDGET_USD).

Covers config parsing, the spend_log data layer, the would_exceed gate, the
orchestrator enforcement (refuse before dispatch, on both the sync and streaming
paths), spend recording for successful AND empty/truncated calls (the folded-in
cost-accounting boundary), and the /v1/status surfacing.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.orchestrator
from app import budget, database
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode
from app.usage import Usage


# --- config parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("0", None),
        ("-5", None),
        ("abc", None),
        ("5", 5.0),
        ("2.50", 2.5),
    ],
)
def test_daily_budget_usd_parsing(
    raw: str | None, expected: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is None:
        monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    else:
        monkeypatch.setenv("DAILY_BUDGET_USD", raw)
    assert budget.daily_budget_usd() == expected


# --- spend_log data layer ----------------------------------------------------


def test_record_and_sum_spend_today(db_path: Path) -> None:
    assert database.spend_today_usd() == 0.0
    database.record_spend("alice", "gpt-5", 100, 200, 0.01)
    database.record_spend(None, "gpt-5-mini", 50, 50, 0.002)
    assert database.spend_today_usd() == pytest.approx(0.012)
    # A NULL cost (unpriced model) must not break the SUM.
    database.record_spend(None, "unpriced", 10, 10, None)
    assert database.spend_today_usd() == pytest.approx(0.012)


# --- would_exceed gate -------------------------------------------------------


def test_would_exceed_none_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    assert budget.would_exceed("gpt-5", 1000) is None


def test_would_exceed_allows_under_budget(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "100")
    assert budget.would_exceed("gpt-5", 1000) is None


def test_would_exceed_blocks_when_already_over(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.01)
    note = budget.would_exceed("gpt-5", 100)
    assert note is not None
    assert "budget" in note.lower()


def test_would_exceed_worst_case_blocks_a_single_costly_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with zero prior spend, a call whose worst-case OUTPUT cost alone
    # exceeds the budget is refused (gpt-5 output 10/1M; 1000 tok -> 0.01 > 0.001).
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")
    assert budget.would_exceed("gpt-5", 1000) is not None


# --- budget_status -----------------------------------------------------------


def test_budget_status_disabled(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    assert budget.budget_status() == {"enabled": False}


def test_budget_status_enabled_withholds_figures(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.0")
    database.record_spend(None, "gpt-5", 100, 100, 0.25)
    # Only the enabled flag is exposed — live spend/limit are withheld from the
    # public status endpoint.
    assert budget.budget_status() == {"enabled": True}


def test_would_exceed_fails_open_on_db_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")

    def boom() -> float:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(budget.database, "spend_today_usd", boom)
    # A transient spend-read failure must not block the request (fail open).
    assert budget.would_exceed("gpt-5", 1000) is None


def test_refusal_note_does_not_disclose_spend_or_limit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.42)
    note = budget.would_exceed("gpt-5", 100)
    assert note is not None
    assert "budget" in note.lower()
    assert "0.42" not in note and "0.005" not in note  # no figures leaked


# --- orchestrator enforcement (sync) -----------------------------------------


def test_run_orchestrator_refuses_when_over_budget(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.01)  # already over

    called = {"hit": False}

    def fake_call_model(**_kwargs: object) -> str:
        called["hit"] = True
        return "should not run"

    monkeypatch.setattr(app.orchestrator, "_call_model", fake_call_model)

    resp = run_orchestrator(AskRequest(question="hi", mode=Mode.fast))

    assert resp.answer == ""
    assert "budget" in resp.notes.lower()
    assert called["hit"] is False  # refused before any model call


def test_run_orchestrator_records_spend_on_success(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)  # no cap

    def fake_call_model(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage: Usage | None = None,
    ) -> str:
        if usage is not None:
            usage.input_tokens = 1000
            usage.output_tokens = 500
        return "answer"

    monkeypatch.setattr(app.orchestrator, "_call_model", fake_call_model)

    assert database.spend_today_usd() == 0.0
    resp = run_orchestrator(AskRequest(question="hi", mode=Mode.fast), owner="alice")

    assert resp.answer == "answer"
    assert database.spend_today_usd() > 0.0  # the call's cost was recorded


# --- orchestrator enforcement (streaming) ------------------------------------


def test_stream_orchestrator_refuses_before_meta_when_over_budget(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.01)

    called = {"hit": False}

    def fake_stream_model(**_kwargs: object) -> Iterator[str]:
        called["hit"] = True
        yield "x"

    monkeypatch.setattr(app.orchestrator, "_stream_model", fake_stream_model)

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))

    assert called["hit"] is False
    assert events[-1]["event"] == "error"
    assert "budget" in events[-1]["data"]["message"].lower()
    assert all(e["event"] != "meta" for e in events)  # refused before meta


def test_empty_streaming_call_still_records_spend(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folded-in boundary: a truncated call yields no text but real usage,
    so its cost must reach the spend log even though no message is persisted.
    """
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)

    def fake_stream_model(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage: Usage | None = None,
    ) -> Iterator[str]:
        if usage is not None:
            usage.input_tokens = 2000
            usage.output_tokens = 4000
        return
        yield  # unreachable — makes this a generator that yields nothing

    monkeypatch.setattr(app.orchestrator, "_stream_model", fake_stream_model)

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))

    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["answer"] == ""  # truncated: empty answer
    assert database.spend_today_usd() > 0.0  # ...but the cost was still recorded


# --- HTTP surfacing ----------------------------------------------------------


def test_ask_endpoint_refused_when_over_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")
    database.record_spend(None, "gpt-5", 100, 100, 1.0)

    called = {"hit": False}

    def fake_call_model(**_kwargs: object) -> str:
        called["hit"] = True
        return "nope"

    monkeypatch.setattr(app.orchestrator, "_call_model", fake_call_model)

    r = client.post("/v1/ask", json={"question": "hi", "mode": "fast"})

    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == ""
    assert "budget" in body["notes"].lower()
    assert called["hit"] is False


def test_status_surfaces_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "2.0")
    database.record_spend(None, "gpt-5", 100, 100, 0.5)

    body = client.get("/v1/status").json()
    # Public status shows only that a cap is active — no live figures.
    assert body["budget"] == {"enabled": True}
    assert "spent_today_usd" not in body["budget"]


def test_status_budget_disabled_by_default(client: TestClient) -> None:
    assert client.get("/v1/status").json()["budget"] == {"enabled": False}


# --- review follow-ups: input-cost estimate + unpriced-model handling --------


def test_would_exceed_counts_input_prompt_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.02")
    # Output alone is cheap (gpt-5 output 10/M; 800 tok ~= $0.008 < 0.02).
    assert budget.would_exceed("gpt-5", 800, "hi") is None
    # A large input prompt (gpt-5 input 1.25/M; ~80k tokens ~= $0.10) tips it over.
    big_prompt = "x" * 320_000  # ~80k tokens at 4 chars/token
    assert budget.would_exceed("gpt-5", 800, big_prompt) is not None


def test_would_exceed_warns_and_allows_unpriced_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("DAILY_BUDGET_USD", "0.01")
    with caplog.at_level(logging.WARNING):
        result = budget.would_exceed("totally-unknown-model", 1000, "hi")
    # Can't cap what we can't price -> fail open, but warn loudly.
    assert result is None
    assert "budget.unpriced_model" in caplog.text
