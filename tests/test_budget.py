"""Global daily spend cap (DAILY_BUDGET_USD).

Covers config parsing, the spend_log data layer, the reserve()/finalize/
release atomic gate, the orchestrator enforcement (refuse before dispatch, on
both the sync and streaming paths), spend recording for successful AND
empty/truncated calls (the folded-in cost-accounting boundary), and the
/v1/status surfacing.
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


def test_try_reserve_spend_admits_and_counts_immediately(db_path: Path) -> None:
    admitted, spent_before, reservation_id = database.try_reserve_spend(
        "alice", "gpt-5", 0.01, 1.0
    )
    assert admitted is True
    assert spent_before == 0.0
    assert reservation_id is not None
    # The reservation itself is visible in today's spend right away, before
    # any finalize/release — this is what closes the check-then-spend race.
    assert database.spend_today_usd() == pytest.approx(0.01)


def test_try_reserve_spend_refuses_over_limit_without_inserting(
    db_path: Path,
) -> None:
    admitted, spent_before, reservation_id = database.try_reserve_spend(
        None, "gpt-5", 5.0, 1.0
    )
    assert admitted is False
    assert reservation_id is None
    assert database.spend_today_usd() == 0.0  # refused: nothing was inserted


def test_finalize_spend_reconciles_the_placeholder(db_path: Path) -> None:
    _admitted, _spent, reservation_id = database.try_reserve_spend(
        "alice", "gpt-5", 0.01, 1.0
    )
    assert reservation_id is not None
    database.finalize_spend(reservation_id, 500, 200, 0.003)
    # The real (lower) cost replaces the worst-case placeholder.
    assert database.spend_today_usd() == pytest.approx(0.003)


def test_release_spend_zeroes_out_an_abandoned_reservation(db_path: Path) -> None:
    _admitted, _spent, reservation_id = database.try_reserve_spend(
        "alice", "gpt-5", 0.01, 1.0
    )
    assert reservation_id is not None
    database.release_spend(reservation_id)
    assert database.spend_today_usd() == 0.0


def test_try_reserve_spend_serializes_concurrent_admissions(db_path: Path) -> None:
    """The actual race this fix closes: two threads reserving at almost the
    same instant must not both be admitted when only one of them fits under
    the cap — a plain read-then-later-write gate can't guarantee that, since
    both reads can happen before either write."""
    import threading

    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        admitted, _spent, _reservation_id = database.try_reserve_spend(
            None, "gpt-5", 0.01, 0.015
        )
        with lock:
            results.append(admitted)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Cap is 0.015; each reservation is 0.01 -> only one of five can fit.
    assert results.count(True) == 1
    assert database.spend_today_usd() == pytest.approx(0.01)


# --- reserve() gate -----------------------------------------------------------


def test_reserve_none_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    note, reservation_id = budget.reserve("gpt-5", 1000)
    assert note is None
    assert reservation_id is None


def test_reserve_allows_under_budget(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "100")
    note, reservation_id = budget.reserve("gpt-5", 1000)
    assert note is None
    assert reservation_id is not None
    # The reservation itself must be visible in today's spend immediately.
    assert database.spend_today_usd() > 0.0


def test_reserve_blocks_when_already_over(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.01)
    note, reservation_id = budget.reserve("gpt-5", 100)
    assert note is not None
    assert "budget" in note.lower()
    assert reservation_id is None


def test_reserve_worst_case_blocks_a_single_costly_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with zero prior spend, a call whose worst-case OUTPUT cost alone
    # exceeds the budget is refused (gpt-5 output 10/1M; 1000 tok -> 0.01 > 0.001).
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")
    note, reservation_id = budget.reserve("gpt-5", 1000)
    assert note is not None
    assert reservation_id is None


def test_reserve_never_blocks_a_free_ollama_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local ollama model prices at $0, so it passes the gate even when
    # recorded spend already sits PAST the cap (overshoot is reachable via
    # fallback dispatch) — free calls can't push the total any further, so
    # refusing them would brick the free tier for the rest of the UTC day.
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.015)  # already over the cap
    note, reservation_id = budget.reserve("ollama/llama3.1:8b", 100_000)
    assert note is None
    assert reservation_id is None  # free: nothing to reserve/reconcile
    # ...but a free base with a known image cost is still real money: gated.
    note, reservation_id = budget.reserve(
        "ollama/llama3.1:8b", 100_000, extra_cost_usd=0.19
    )
    assert note is not None
    assert reservation_id is None


def test_reserve_admits_concurrent_calls_without_double_counting(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The actual bug being fixed: two calls whose combined worst case exceeds
    # the cap must not BOTH be admitted, even though neither one's spend was
    # recorded yet at the moment the second one is evaluated — the old
    # would_exceed-then-later-record_spend pattern read the same stale total
    # for both. reserve() writes the first admission's placeholder into the
    # total immediately, so the second call sees it and is refused.
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.015")
    # gpt-5 output 10/1M; 1000 tok -> $0.01 worst case each; two would be $0.02.
    note1, reservation1 = budget.reserve("gpt-5", 1000)
    assert note1 is None
    assert reservation1 is not None
    note2, reservation2 = budget.reserve("gpt-5", 1000)
    assert note2 is not None  # refused: $0.01 already reserved + $0.01 > $0.015
    assert reservation2 is None


# --- per-owner daily cap -------------------------------------------------------


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
def test_daily_budget_per_owner_usd_parsing(
    raw: str | None, expected: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is None:
        monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    else:
        monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", raw)
    assert budget.daily_budget_per_owner_usd() == expected


def test_reserve_blocks_on_per_owner_cap_alone(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No global cap configured at all — only the per-owner one applies.
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.005")
    database.record_spend("alice", "gpt-5", 100, 100, 0.01)  # alice already over

    note, reservation_id = budget.reserve("gpt-5", 100, owner="alice")
    assert note is not None
    assert reservation_id is None


def test_reserve_per_owner_cap_does_not_block_a_different_owner(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.005")
    database.record_spend("alice", "gpt-5", 100, 100, 0.01)  # alice already over

    # Bob's own spend is $0 — his own cap has plenty of room, even though
    # alice's is exhausted. The per-owner cap must not act like a global one.
    note, reservation_id = budget.reserve("gpt-5", 100, owner="bob")
    assert note is None
    assert reservation_id is not None


def test_reserve_per_owner_cap_scopes_the_shared_none_bucket_on_its_own(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # owner=None (static-token / no-auth deployments) is its own distinct
    # scope, same as everywhere else owner scoping is enforced (see
    # test_make_key_owner_none_is_its_own_distinct_scope in test_cache.py).
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.005")
    database.record_spend("alice", "gpt-5", 100, 100, 0.01)  # alice already over

    note, reservation_id = budget.reserve("gpt-5", 100, owner=None)
    assert note is None
    assert reservation_id is not None


def test_reserve_enforces_both_caps_whichever_is_hit_first(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Global cap has room, but alice's own per-owner cap does not.
    monkeypatch.setenv("DAILY_BUDGET_USD", "100")
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.005")
    database.record_spend("alice", "gpt-5", 100, 100, 0.01)

    note, reservation_id = budget.reserve("gpt-5", 100, owner="alice")
    assert note is not None
    assert reservation_id is None


def test_reserve_global_cap_still_applies_even_with_per_owner_room(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # alice's own per-owner cap has plenty of room, but the GLOBAL total
    # (driven by other owners' spend) is already exhausted.
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "100")
    database.record_spend(
        "bob", "gpt-5", 100, 100, 0.01
    )  # bob alone busts the global cap

    note, reservation_id = budget.reserve("gpt-5", 100, owner="alice")
    assert note is not None
    assert reservation_id is None


def test_reserve_admits_when_both_caps_have_room(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "100")
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "100")

    note, reservation_id = budget.reserve("gpt-5", 100, owner="alice")
    assert note is None
    assert reservation_id is not None


def test_try_reserve_spend_enforces_owner_limit_directly(db_path: Path) -> None:
    admitted, spent, reservation_id = database.try_reserve_spend(
        "alice", "gpt-5", 0.01, limit_usd=None, owner_limit_usd=0.005
    )
    assert admitted is False
    assert reservation_id is None

    admitted, spent, reservation_id = database.try_reserve_spend(
        "alice", "gpt-5", 0.01, limit_usd=None, owner_limit_usd=0.02
    )
    assert admitted is True
    assert reservation_id is not None


# --- budget_status -----------------------------------------------------------


def test_budget_status_disabled(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    assert budget.budget_status() == {"enabled": False, "per_owner_enabled": False}


def test_budget_status_enabled_withholds_figures(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "1.0")
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    database.record_spend(None, "gpt-5", 100, 100, 0.25)
    # Only the enabled flags are exposed — live spend/limits are withheld from
    # the public status endpoint.
    assert budget.budget_status() == {"enabled": True, "per_owner_enabled": False}


def test_budget_status_reports_per_owner_cap_independently(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.5")
    assert budget.budget_status() == {"enabled": False, "per_owner_enabled": True}


def test_reserve_fails_open_on_db_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.001")

    def boom(*_args: object, **_kwargs: object) -> float:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(budget.database, "try_reserve_spend", boom)
    # A transient spend-read/write failure must not block the request (fail open).
    note, reservation_id = budget.reserve("gpt-5", 1000)
    assert note is None
    assert reservation_id is None


def test_refusal_note_does_not_disclose_spend_or_limit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.42)
    note, reservation_id = budget.reserve("gpt-5", 100)
    assert note is not None
    assert "budget" in note.lower()
    assert "0.42" not in note and "0.005" not in note  # no figures leaked
    assert reservation_id is None


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
        cacheable_system: object = None,
        anthropic_question: object = None,
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


def test_dead_free_primary_cannot_route_paid_fallback_past_the_cap(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flagship free-tier failure mode: a $0 Ollama primary passes the
    # pre-dispatch gate even with the cap exhausted, then turns out to be
    # down. The fallback chain (all paid) must be re-gated per candidate —
    # otherwise the failure of a FREE model routes PAID spend past the cap.
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.005")
    database.record_spend(None, "gpt-5", 100, 100, 0.01)  # cap exhausted

    attempted: list[str] = []

    def fake_call_model(*, model: str, **_kwargs: object) -> str:
        attempted.append(model)
        if model.startswith("ollama/"):
            raise ConnectionError("Ollama server is not running")
        return "paid answer that must never happen"

    monkeypatch.setattr(app.orchestrator, "_call_model", fake_call_model)

    resp = run_orchestrator(AskRequest(question="hi", model="ollama/llama3.1:8b"))

    assert attempted == ["ollama/llama3.1:8b"]  # no paid fallback dispatched
    assert resp.answer == ""


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
        cacheable_system: object = None,
        anthropic_question: object = None,
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
    assert body["budget"] == {"enabled": True, "per_owner_enabled": False}
    assert "spent_today_usd" not in body["budget"]


def test_status_budget_disabled_by_default(client: TestClient) -> None:
    assert client.get("/v1/status").json()["budget"] == {
        "enabled": False,
        "per_owner_enabled": False,
    }


# --- review follow-ups: input-cost estimate + unpriced-model handling --------


def test_reserve_counts_input_prompt_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.02")
    # Output alone is cheap (gpt-5 output 10/M; 800 tok ~= $0.008 < 0.02).
    assert budget.reserve("gpt-5", 800, "hi")[0] is None
    # A large input prompt (gpt-5 input 1.25/M; ~80k tokens ~= $0.10) tips it over.
    big_prompt = "x" * 320_000  # ~80k tokens at 4 chars/token
    assert budget.reserve("gpt-5", 800, big_prompt)[0] is not None


def test_reserve_warns_and_allows_unpriced_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("DAILY_BUDGET_USD", "0.01")
    with caplog.at_level(logging.WARNING):
        note, reservation_id = budget.reserve("totally-unknown-model", 1000, "hi")
    # Can't cap what we can't price -> fail open, but warn loudly.
    assert note is None
    assert reservation_id is None
    assert "budget.unpriced_model" in caplog.text


# --- fix: image-generation cost folded into the pre-dispatch estimate --------


def test_reserve_counts_extra_cost_usd(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.02")
    # Token cost alone is well under the cap.
    assert budget.reserve("gpt-5", 100, "hi")[0] is None
    # The same call plus a $0.19 image (default "high" quality estimate) tips
    # it over — this is exactly the gap: image generation cost is real money
    # that the token-only estimate used to miss entirely.
    assert budget.reserve("gpt-5", 100, "hi", 0.19)[0] is not None


def test_reserve_extra_cost_usd_defaults_to_zero_no_behavior_change(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.02")
    assert (
        budget.reserve("gpt-5", 800, "hi")[0]
        == budget.reserve("gpt-5", 800, "hi", 0.0)[0]
    )


def test_reserve_enforces_known_image_cost_even_for_unpriced_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TOKEN cost of an unpriced model can't be bounded, but a known image
    cost is still real money and must still be enforced on its own — not a
    reason to let the whole call through unbounded."""
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.10")
    assert budget.reserve("totally-unknown-model", 1000, "hi", 0.19)[0] is not None


def test_reserve_unpriced_model_no_image_cost_still_fails_open(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.10")
    note, reservation_id = budget.reserve("totally-unknown-model", 1000, "hi", 0.0)
    assert note is None
    assert reservation_id is None


def test_worst_case_image_cost_zero_when_neither_gate_active() -> None:
    assert app.orchestrator._worst_case_image_cost(False, False) == 0.0


def test_worst_case_image_cost_nonzero_for_openai_tool_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    assert app.orchestrator._worst_case_image_cost(True, False) == pytest.approx(0.19)


def test_worst_case_image_cost_nonzero_for_gemini_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "low")
    assert app.orchestrator._worst_case_image_cost(False, True) == pytest.approx(0.02)


def test_run_orchestrator_passes_image_cost_to_reserve(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setattr(app.orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(app.orchestrator, "_call_model", lambda **_kw: "ok")

    seen = {}

    def fake_reserve(
        model, max_output_tokens, prompt="", extra_cost_usd=0.0, owner=None
    ):
        seen["extra_cost_usd"] = extra_cost_usd
        return None, None

    monkeypatch.setattr(budget, "reserve", fake_reserve)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["extra_cost_usd"] == pytest.approx(0.19)


def test_run_orchestrator_zero_image_cost_when_feature_off(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    monkeypatch.setattr(app.orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(app.orchestrator, "_call_model", lambda **_kw: "ok")

    seen = {}

    def fake_reserve(
        model, max_output_tokens, prompt="", extra_cost_usd=0.0, owner=None
    ):
        seen["extra_cost_usd"] = extra_cost_usd
        return None, None

    monkeypatch.setattr(budget, "reserve", fake_reserve)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["extra_cost_usd"] == 0.0


def test_stream_orchestrator_passes_image_cost_to_reserve(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setattr(app.orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(app.orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    seen = {}

    def fake_reserve(
        model, max_output_tokens, prompt="", extra_cost_usd=0.0, owner=None
    ):
        seen["extra_cost_usd"] = extra_cost_usd
        return None, None

    monkeypatch.setattr(budget, "reserve", fake_reserve)

    list(stream_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart)))
    assert seen["extra_cost_usd"] == pytest.approx(0.19)


def test_run_orchestrator_refuses_when_image_cost_alone_exceeds_budget(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: this is the actual gap being fixed. Before this fix, an
    image-generating call would sail through the pre-dispatch gate as long as
    its TOKEN cost alone fit the cap, since image cost was never priced. Here
    the token cost (smart tier, ~4000 output tokens @ gpt-5 rates ~= $0.04)
    fits comfortably under $0.10, but adding the ~$0.19 "high" quality image
    estimate pushes the same call over — so refusal can only be explained by
    the image cost actually being counted now."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.10")
    monkeypatch.setattr(app.orchestrator, "get_client", lambda: object())

    def boom(**_kw):
        raise AssertionError("must not dispatch once the budget gate refuses")

    monkeypatch.setattr(app.orchestrator, "_call_model", boom)

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert result.answer == ""
    assert "budget" in result.notes.lower()
