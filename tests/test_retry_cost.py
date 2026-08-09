"""Re-run cost: attribution (app/retry_attribution.py, retry_log) and the
report over it (app/retry_cost.py).

The property under test throughout is that a retry's cost lands on the
ORIGINAL routing decision for that turn — the thing the schema destroyed
before retry_log existed, since regenerate deletes the answer it replaces and
edit deletes the user turn too. Measurement only: nothing here asserts (or
permits) a change in routing behaviour.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages as _messages
from app import database, retry_attribution, retry_cost
from app.schemas import AskRequest, AskResponse

# --- harness -----------------------------------------------------------------


def _stub_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[dict[str, Any]],
) -> list[AskRequest]:
    """Answer each successive call with the next entry of `answers` (the last
    one repeating), so a test can give the first attempt one routing decision
    and cost and its retry another."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        recall_library: bool = False,
        memory_sources: list[dict] | None = None,
        forced_category: str | None = None,
    ) -> AskResponse:
        spec = answers[min(len(calls), len(answers) - 1)]
        calls.append(req)
        return AskResponse(
            answer=spec.get("answer", f"answer {len(calls)}"),
            mode_used=spec.get("mode_used", "auto->fast:coding"),
            notes="n",
            model=spec.get("model", "gpt-5-mini"),
            cost_usd=spec.get("cost_usd", 0.01),
        )

    monkeypatch.setattr(_messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _ask(client: TestClient, cid: int, question: str = "hello") -> None:
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask", json={"question": question}
        ).status_code
        == 200
    )


def _regenerate(client: TestClient, cid: int) -> None:
    assert (
        client.post(f"/v1/conversations/{cid}/regenerate", json={}).status_code == 200
    )


def _messages_of(client: TestClient, cid: int) -> list[dict[str, Any]]:
    return list(client.get(f"/v1/conversations/{cid}/messages").json())


def _rows(db_path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM retry_log ORDER BY id").fetchall()
        ]


# --- schema ------------------------------------------------------------------


def test_fresh_db_has_the_retry_log_table_and_indexes(db_path) -> None:
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(retry_log)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(retry_log)")}
    assert columns == {
        "id",
        "owner",
        "conversation_id",
        "turn_key",
        "user_message_id",
        "message_id",
        "attempt_index",
        "signal",
        "mode_used",
        "model",
        "category",
        "tier",
        "cost_usd",
        "created_at",
    }
    assert "idx_retry_log_created_at" in indexes
    assert "idx_retry_log_turn_key" in indexes


# --- classify_signal ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "feedback", "expected"),
    [
        ("regenerate", None, retry_attribution.SIGNAL_REGENERATED_UNRATED),
        ("regenerate", -1, retry_attribution.SIGNAL_REGENERATED_AFTER_DOWNVOTE),
        ("regenerate", 1, retry_attribution.SIGNAL_REGENERATED_AFTER_UPVOTE),
        ("edit", None, retry_attribution.SIGNAL_EDITED),
        # An edit is an edit whatever the rating was — the user rewriting their
        # own prompt is the stronger signal.
        ("edit", -1, retry_attribution.SIGNAL_EDITED),
    ],
)
def test_classify_signal_keeps_the_reasons_apart(
    kind: str, feedback: int | None, expected: str
) -> None:
    assert retry_attribution.classify_signal(kind, feedback) == expected


# --- attribution: regenerate -------------------------------------------------


def test_regenerate_records_the_original_attempt_and_the_retry(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(
        monkeypatch,
        [
            {"mode_used": "auto->fast:coding", "cost_usd": 0.01},
            {"mode_used": "auto->smart:coding", "cost_usd": 0.09},
        ],
    )
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    rows = _rows(db_path)
    assert len(rows) == 2
    original, retry = rows
    # The original answer is recorded RETROACTIVELY (it was deleted by the
    # regenerate) with its own routing decision and cost intact.
    assert original["attempt_index"] == 1
    assert original["signal"] is None
    assert original["mode_used"] == "auto->fast:coding"
    assert original["category"] == "coding"
    assert original["tier"] == "fast"
    assert original["cost_usd"] == pytest.approx(0.01)
    assert retry["attempt_index"] == 2
    assert retry["signal"] == retry_attribution.SIGNAL_REGENERATED_UNRATED
    assert retry["cost_usd"] == pytest.approx(0.09)
    # Both attempts hang off the same turn — the surviving user message.
    user_id = int(
        next(m for m in _messages_of(client, cid) if m["role"] == "user")["id"]
    )
    assert original["turn_key"] == retry["turn_key"] == user_id


def test_a_second_regenerate_appends_only_the_new_attempt(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)
    _regenerate(client, cid)

    rows = _rows(db_path)
    assert [row["attempt_index"] for row in rows] == [1, 2, 3]
    assert [row["signal"] for row in rows] == [
        None,
        retry_attribution.SIGNAL_REGENERATED_UNRATED,
        retry_attribution.SIGNAL_REGENERATED_UNRATED,
    ]
    # One turn, three attempts — not three turns.
    assert len({row["turn_key"] for row in rows}) == 1


def test_a_regenerate_after_a_thumbs_down_is_recorded_as_a_quality_failure(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    message_id = int(
        next(m for m in _messages_of(client, cid) if m["role"] == "assistant")["id"]
    )
    assert (
        client.put(
            f"/v1/conversations/{cid}/messages/{message_id}/feedback",
            json={"verdict": "down"},
        ).status_code
        == 200
    )

    _regenerate(client, cid)

    rows = _rows(db_path)
    assert rows[-1]["signal"] == retry_attribution.SIGNAL_REGENERATED_AFTER_DOWNVOTE


def test_a_failed_regeneration_records_nothing(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer replaces nothing (the old answer is kept), so there is
    no retry to attribute — see retry_attribution's KNOWN LIMITS."""
    _stub_orchestrator(
        monkeypatch, [{"cost_usd": 0.01}, {"answer": "", "cost_usd": 0.02}]
    )
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    assert _rows(db_path) == []


def test_regenerating_a_turn_with_no_answer_records_nothing(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    user_id = int(
        next(m for m in _messages_of(client, cid) if m["role"] == "user")["id"]
    )
    database.delete_messages_after(cid, user_id)

    _regenerate(client, cid)

    assert _rows(db_path) == []


# --- attribution: edit and the chain across it -------------------------------


def test_edit_records_the_retry_and_keeps_the_chain_across_the_new_user_row(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid, "first phrasing")
    user_id = int(
        next(m for m in _messages_of(client, cid) if m["role"] == "user")["id"]
    )

    assert (
        client.post(
            f"/v1/conversations/{cid}/messages/{user_id}/edit",
            json={"question": "clearer phrasing"},
        ).status_code
        == 200
    )
    # ...then regenerate the edited turn: the second retry must extend the SAME
    # chain, even though the edit deleted and re-created the user row.
    _regenerate(client, cid)

    rows = _rows(db_path)
    assert [row["attempt_index"] for row in rows] == [1, 2, 3]
    assert [row["signal"] for row in rows] == [
        None,
        retry_attribution.SIGNAL_EDITED,
        retry_attribution.SIGNAL_REGENERATED_UNRATED,
    ]
    assert len({row["turn_key"] for row in rows}) == 1
    new_user_id = int(
        next(m for m in _messages_of(client, cid) if m["role"] == "user")["id"]
    )
    assert new_user_id != user_id
    assert rows[-1]["user_message_id"] == new_user_id


# --- attribution: streaming --------------------------------------------------


def test_the_streaming_regenerate_records_the_same_attribution(
    client: TestClient, db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, **kwargs):
        yield {"event": "meta", "data": {"mode_used": "auto->fast:coding"}}
        yield {
            "event": "done",
            "data": {
                "answer": "streamed",
                "mode_used": "auto->fast:coding",
                "notes": "n",
                "model": "gpt-5-mini",
                "cost_usd": 0.04,
            },
        }

    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    monkeypatch.setattr(_messages, "stream_orchestrator", fake_stream)

    with client.stream(
        "POST", f"/v1/conversations/{cid}/regenerate/stream", json={}
    ) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass

    rows = _rows(db_path)
    assert [row["attempt_index"] for row in rows] == [1, 2]
    assert rows[1]["signal"] == retry_attribution.SIGNAL_REGENERATED_UNRATED
    assert rows[1]["cost_usd"] == pytest.approx(0.04)


# --- summarize ---------------------------------------------------------------


def test_summarize_reports_first_attempt_and_true_cost_per_category_and_tier(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(
        monkeypatch,
        [
            {"mode_used": "auto->fast:coding", "cost_usd": 0.01},
            {"mode_used": "auto->smart:coding", "cost_usd": 0.09},
        ],
    )
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    summary = retry_cost.summarize(None, days=1)
    overall = summary["overall"]
    assert overall["turns"] == 1
    assert overall["retried_turns"] == 1
    assert overall["retries"] == 1
    assert overall["first_attempt_cost_usd"] == pytest.approx(0.01)
    assert overall["total_cost_usd"] == pytest.approx(0.10)
    assert overall["retry_cost_usd"] == pytest.approx(0.09)
    assert overall["cost_multiplier"] == pytest.approx(10.0)

    # The retry ran on the SMART tier, but the whole turn is booked against the
    # decision that started it — otherwise the dear model that cleaned up would
    # carry the overrun, which reads as an argument for more cheap routing.
    assert set(summary["by_tier"]) == {"fast"}
    assert summary["by_tier"]["fast"]["total_cost_usd"] == pytest.approx(0.10)
    assert summary["by_category"]["coding"]["total_cost_usd"] == pytest.approx(0.10)


def test_summarize_counts_turns_that_were_never_retried(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.02}])
    cid = _create(client)
    _ask(client, cid, "one")
    _ask(client, cid, "two")
    _regenerate(client, cid)

    summary = retry_cost.summarize(None, days=1)
    overall = summary["overall"]
    # Two turns, one of them retried once — the retried turn is counted from
    # the ledger and the other from `messages`, with no double-counting.
    assert overall["turns"] == 2
    assert overall["retried_turns"] == 1
    assert overall["retry_rate"] == pytest.approx(0.5)
    assert overall["first_attempt_cost_usd"] == pytest.approx(0.04)
    assert overall["total_cost_usd"] == pytest.approx(0.06)


def test_summarize_splits_the_signals_rather_than_summing_them(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid, "unrated turn")
    _regenerate(client, cid)

    cid2 = _create(client, "t2")
    _ask(client, cid2, "rated turn")
    message_id = int(
        next(m for m in _messages_of(client, cid2) if m["role"] == "assistant")["id"]
    )
    client.put(
        f"/v1/conversations/{cid2}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )
    _regenerate(client, cid2)

    by_signal = retry_cost.summarize(None, days=1)["by_signal"]
    assert by_signal[retry_attribution.SIGNAL_REGENERATED_UNRATED]["retries"] == 1
    assert (
        by_signal[retry_attribution.SIGNAL_REGENERATED_AFTER_DOWNVOTE]["retries"] == 1
    )
    # Every signal is always present, so a reader sees the zeroes too rather
    # than one aggregated "retries" number.
    assert set(by_signal) == set(retry_attribution.SIGNALS)


def test_summarize_excludes_the_apps_own_system_report_from_the_denominator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    report_cid = int(database.create_conversation("📊 System report", None)["id"])
    database.add_message(
        report_cid, role="assistant", content="x", mode_used="self_report"
    )

    assert retry_cost.summarize(None, days=1)["overall"]["turns"] == 1


def test_summarize_attributes_a_correction_to_the_original_decision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction flagged against attempt 2 belongs to the decision that
    started the turn, not to the model that answered second."""
    _stub_orchestrator(
        monkeypatch,
        [
            {"mode_used": "auto->fast:coding", "cost_usd": 0.01},
            {"mode_used": "auto->smart:research", "cost_usd": 0.09},
        ],
    )
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)
    _ask(client, cid, "That's not what I asked.")

    summary = retry_cost.summarize(None, days=1)
    assert database.correction_log_entries(None, days=1)  # the flag was raised
    assert summary["by_category"]["coding"]["corrections"] == 1
    assert summary["by_tier"]["fast"]["corrections"] == 1
    # The flag was raised against attempt 2, which routed to smart:research —
    # that category is present (the correcting message is its own turn) but
    # carries none of the blame for the turn it was correcting.
    assert summary["by_category"]["research"]["corrections"] == 0
    assert summary["by_tier"]["smart"]["corrections"] == 0


# --- small-n treatment -------------------------------------------------------


def test_a_rate_from_a_tiny_sample_reads_as_insufficient() -> None:
    interval = retry_cost.wilson_interval(2, 5)
    assert interval is not None
    low, high = interval
    # The interval spans "fine" and "failing" — the figure cannot separate them.
    assert low < 0.2 < 0.6 < high
    assert high - low > retry_cost._DIRECTIONAL_WIDTH


def test_a_zero_rate_from_a_tiny_sample_is_not_reported_as_certainty() -> None:
    interval = retry_cost.wilson_interval(0, 5)
    assert interval is not None
    # The normal approximation would give +/-0 here, i.e. false certainty.
    assert interval[0] == 0.0
    assert interval[1] > 0.3


def test_wilson_interval_needs_something_to_bound() -> None:
    assert retry_cost.wilson_interval(0, 0) is None
    assert retry_cost.turns_for_directional(0, 0) is None


def test_turns_for_directional_says_how_much_data_the_rate_would_need() -> None:
    needed = retry_cost.turns_for_directional(2, 5)
    assert needed is not None and needed > 5
    interval = retry_cost.wilson_interval(round(0.4 * needed), needed)
    assert interval is not None
    assert interval[1] - interval[0] <= retry_cost._DIRECTIONAL_WIDTH
    # A sample already inside the guardrail is reported as needing no more.
    assert retry_cost.turns_for_directional(40, 100) == 100


def test_every_stat_carries_its_n_and_its_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    summary = retry_cost.summarize(None, days=1)
    for stat in [
        summary["overall"],
        *summary["by_category"].values(),
        *summary["by_tier"].values(),
    ]:
        assert "turns" in stat
        assert stat["reads_as"] == "insufficient"
        assert stat["retry_rate_ci"] is not None
    assert retry_cost.summarize(None, days=1)["overall"]["reads_as"] != "directional"


def test_an_empty_window_reads_as_no_data(db_path) -> None:
    summary = retry_cost.summarize(None, days=1)
    assert summary["overall"]["turns"] == 0
    assert summary["overall"]["reads_as"] == "no_data"
    assert summary["overall"]["retry_rate_ci"] is None
    assert summary["overall"]["cost_multiplier"] is None


def test_an_unpriced_attempt_is_counted_so_a_zero_total_is_not_read_as_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": None}])
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    overall = retry_cost.summarize(None, days=1)["overall"]
    assert overall["unpriced_attempts"] == 2
    assert overall["total_cost_usd"] == pytest.approx(0.0)


# --- endpoint ----------------------------------------------------------------


def test_the_summary_endpoint_returns_the_same_shape(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": 0.01}])
    cid = _create(client)
    _ask(client, cid)
    _regenerate(client, cid)

    body = client.get("/v1/retry-cost/summary?days=1").json()
    assert body["overall"]["retried_turns"] == 1
    assert body["overall"]["retry_rate_ci"] is not None
    assert body["overall"]["reads_as"] == "insufficient"
    assert set(body["by_signal"]) == set(retry_attribution.SIGNALS)
    assert body["by_tier"]["fast"]["turns"] == 1


def test_the_summary_endpoint_rejects_an_out_of_range_window(
    client: TestClient,
) -> None:
    assert client.get("/v1/retry-cost/summary?days=0").status_code == 422
    assert client.get("/v1/retry-cost/summary?days=91").status_code == 422


# --- the remaining branches of the small-n treatment --------------------------


def test_a_rate_with_enough_turns_reads_as_directional(db_path) -> None:
    """The other side of the guardrail: with a real sample the same figure is
    reported as a direction rather than as "too few to be a finding"."""
    conv = database.create_conversation("t", None)
    for _ in range(100):
        database.add_message(
            int(conv["id"]),
            "assistant",
            "a",
            mode_used="auto->fast:coding",
            model="gpt-5-mini",
            cost_usd=0.01,
        )

    overall = retry_cost.summarize(None, days=1)["overall"]
    assert overall["turns"] == 100
    assert overall["reads_as"] == "directional"
    assert overall["turns_for_directional"] == 100


def test_turns_for_directional_gives_up_rather_than_promising_a_huge_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the cap the honest answer is "more than this deployment will
    produce", not a precise number that reads as reachable."""
    monkeypatch.setattr(retry_cost, "_MAX_PROJECTED_TURNS", 10)
    assert retry_cost.turns_for_directional(2, 5) is None


def test_a_correction_against_an_answer_outside_the_window_keeps_its_own_route(
    db_path,
) -> None:
    """correction_log carries its own model/mode_used/category snapshot for
    exactly this case — the flagged answer is gone, or predates the window, so
    there is no turn here to re-attribute it to."""
    database.record_correction_flag(
        owner=None,
        message_id=999_999,
        model="gpt-5-mini",
        mode_used="auto->smart:analysis",
        category="analysis",
    )

    summary = retry_cost.summarize(None, days=1)
    assert summary["overall"]["corrections"] == 1
    assert summary["by_category"]["analysis"]["corrections"] == 1
    assert summary["by_tier"]["smart"]["corrections"] == 1


def test_a_turn_with_no_resolvable_category_still_counts_in_the_overall(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same convention as feedback/correction: an unresolvable dimension is
    absent from that breakdown, never a bucket named "unknown", and the turn
    still counts everywhere it does resolve."""
    _stub_orchestrator(monkeypatch, [{"mode_used": "auto->fast", "cost_usd": 0.02}])
    cid = _create(client)
    _ask(client, cid)

    summary = retry_cost.summarize(None, days=1)
    assert summary["overall"]["turns"] == 1
    assert summary["by_category"] == {}
    assert summary["by_tier"]["fast"]["turns"] == 1


def test_an_unpriced_answer_that_was_never_retried_is_counted_as_unpriced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_orchestrator(monkeypatch, [{"cost_usd": None}])
    cid = _create(client)
    _ask(client, cid)

    overall = retry_cost.summarize(None, days=1)["overall"]
    assert overall["turns"] == 1
    assert overall["unpriced_attempts"] == 1
    assert overall["total_cost_usd"] == pytest.approx(0.0)
