"""Weekly self-report (app/self_report.py): stats math against seeded
ledgers, per-owner staleness scheduling, the zero-LLM-by-default contract,
SELF_REPORT_NARRATE making exactly one router call, and owner scoping.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import database, self_report

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _insert_spend(db_path: Path, owner: str | None, model: str, cost: float) -> None:
    database.record_spend(owner, model, 100, 50, cost)


def _insert_avoided_cost(
    db_path: Path, owner: str | None, model: str, reason: str, cost: float
) -> None:
    database.record_avoided_cost(owner, model, reason, cost)


def _insert_feedback(
    db_path: Path, owner: str | None, model: str, category: str | None, verdict: int
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO feedback_log (owner, message_id, model, mode_used, "
            "category, verdict, reason) VALUES (?, 1, ?, 'auto->fast', ?, ?, NULL)",
            (owner, model, category, verdict),
        )


def _insert_correction(
    db_path: Path,
    owner: str | None,
    message_id: int,
    model: str,
    mode_used: str,
    category: str | None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO correction_log (owner, message_id, model, mode_used, "
            "category) VALUES (?, ?, ?, ?, ?)",
            (owner, message_id, model, mode_used, category),
        )


def _insert_fallback(
    db_path: Path, owner: str | None, model: str, reason: str, succeeded: bool
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO fallback_log (owner, model, reason, succeeded) "
            "VALUES (?, ?, ?, ?)",
            (owner, model, reason, 1 if succeeded else 0),
        )


# --- is_due / staleness scheduling -------------------------------------------


def test_is_due_true_on_first_run(db_path: Path) -> None:
    assert self_report.is_due("alice") is True


def test_is_due_false_immediately_after_a_run_then_true_after_a_week(
    db_path: Path,
) -> None:
    database.record_self_report_run("alice")
    real_now = datetime.now(timezone.utc)
    assert self_report.is_due("alice", now=real_now) is False
    assert self_report.is_due("alice", now=real_now + timedelta(days=8)) is True


def test_is_due_is_tracked_per_owner(db_path: Path) -> None:
    database.record_self_report_run("alice")
    real_now = datetime.now(timezone.utc)
    assert self_report.is_due("alice", now=real_now) is False
    # bob has never had a report generated -- his own clock, unaffected by
    # alice's run.
    assert self_report.is_due("bob", now=real_now) is True


def test_is_due_treats_none_owner_as_its_own_bucket(db_path: Path) -> None:
    database.record_self_report_run(None)
    real_now = datetime.now(timezone.utc)
    assert self_report.is_due(None, now=real_now) is False
    assert self_report.is_due("alice", now=real_now) is True


def test_generate_if_due_noop_second_time(db_path: Path) -> None:
    _insert_spend(db_path, "alice", "gpt-5", 0.10)
    first = self_report.generate_if_due("alice")
    assert first is not None
    second = self_report.generate_if_due("alice")
    assert second is None


def test_generate_if_due_skips_a_meaningless_empty_week(db_path: Path) -> None:
    """An owner with zero spend/avoided-cost/feedback/tool-usage this week
    gets no automatic report -- most commonly hit on a brand-new install's
    very first sidebar load, before any real usage exists."""
    result = self_report.generate_if_due("alice")
    assert result is None
    # And it does NOT record a run either, so a report generates promptly
    # once there's real activity rather than waiting out a further week.
    assert self_report.is_due("alice") is True


def test_generate_report_always_generates_even_for_an_empty_week(
    db_path: Path,
) -> None:
    """Unlike generate_if_due, the "Generate now" button's entry point never
    skips an empty week -- an explicit click is its own signal."""
    result = self_report.generate_report("alice")
    assert result is not None


# --- compile_stats math against seeded ledgers -------------------------------


def test_compile_stats_spend_and_cache_hit_rates(db_path: Path) -> None:
    _insert_spend(db_path, "alice", "gpt-5", 0.10)
    _insert_avoided_cost(db_path, "alice", "gpt-5", "response_cache_hit", 0.02)
    _insert_avoided_cost(db_path, "alice", "gpt-5", "semantic_cache_hit", 0.03)
    _insert_avoided_cost(db_path, "alice", "groq/llama-3", "free_tier", 0.05)

    stats = self_report.compile_stats("alice", days=7)

    assert stats["spend_usd"] == pytest.approx(0.10)
    assert stats["avoided_cost_usd"] == pytest.approx(0.10)
    # total_requests = 1 real spend_log call + 1 exact hit + 1 semantic hit
    assert stats["total_requests"] == 3
    assert stats["exact_cache_hits"] == 1
    assert stats["semantic_cache_hits"] == 1
    assert stats["exact_cache_hit_rate"] == pytest.approx(1 / 3)
    assert stats["semantic_cache_hit_rate"] == pytest.approx(1 / 3)
    assert stats["free_lane_calls"] == 1
    assert stats["free_lane_avoided_cost_usd"] == pytest.approx(0.05)


def test_compile_stats_cache_hit_rate_is_none_with_no_requests(db_path: Path) -> None:
    stats = self_report.compile_stats("alice", days=7)
    assert stats["total_requests"] == 0
    assert stats["exact_cache_hit_rate"] is None
    assert stats["semantic_cache_hit_rate"] is None


def test_compile_stats_feedback_by_model_and_category(db_path: Path) -> None:
    _insert_feedback(db_path, "alice", "gpt-5", "coding", 1)
    _insert_feedback(db_path, "alice", "gpt-5", "coding", -1)
    _insert_feedback(db_path, "alice", "claude-sonnet-5", "creative_writing", 1)

    stats = self_report.compile_stats("alice", days=7)

    assert stats["feedback_by_model"]["gpt-5"]["answers_rated"] == 2
    assert stats["feedback_by_model"]["gpt-5"]["down"] == 1
    assert stats["feedback_by_model"]["gpt-5"]["down_rate"] == pytest.approx(0.5)
    assert stats["feedback_by_category"]["coding"]["answers_rated"] == 2
    assert stats["feedback_by_category"]["creative_writing"]["down_rate"] == 0.0


def test_compile_stats_correction_overall_and_by_dimension(db_path: Path) -> None:
    conv = database.create_conversation("t", "alice")
    m1 = database.add_message(
        conv["id"], "assistant", "answer", mode_used="auto->fast:coding", model="gpt-5"
    )
    database.add_message(
        conv["id"], "assistant", "answer2", mode_used="auto->fast:coding", model="gpt-5"
    )
    _insert_correction(
        db_path, "alice", int(m1["id"]), "gpt-5", "auto->fast:coding", "coding"
    )

    stats = self_report.compile_stats("alice", days=7)

    assert stats["correction_overall"]["flagged"] == 1
    assert stats["correction_overall"]["answers"] == 2
    assert stats["correction_overall"]["correction_rate"] == pytest.approx(0.5)
    assert stats["correction_by_model"]["gpt-5"]["flagged"] == 1
    assert stats["correction_by_category"]["coding"]["flagged"] == 1
    assert stats["correction_by_lane"]["fast"]["flagged"] == 1


def test_compile_stats_correction_is_owner_scoped(db_path: Path) -> None:
    conv = database.create_conversation("t", "alice")
    m1 = database.add_message(
        conv["id"], "assistant", "answer", mode_used="auto->fast", model="gpt-5"
    )
    _insert_correction(db_path, "alice", int(m1["id"]), "gpt-5", "auto->fast", None)

    bob_stats = self_report.compile_stats("bob", days=7)
    assert bob_stats["correction_overall"]["flagged"] == 0


def test_compile_stats_correction_reconciles_across_the_retention_boundary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction flag rolled up and pruned out of correction_log must
    still be counted by a wide-enough report window — same reconciliation
    app/retention.py already guarantees for spend/feedback."""
    from app import retention

    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    conv = database.create_conversation("t", "alice")
    m1 = database.add_message(
        conv["id"], "assistant", "answer", mode_used="auto->fast", model="gpt-5"
    )
    _insert_correction(db_path, "alice", int(m1["id"]), "gpt-5", "auto->fast", None)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE correction_log SET created_at = datetime('now', '-60 days')"
        )

    pruned = retention.rollup_and_prune()
    assert pruned["correction_log"] == 1

    stats = self_report.compile_stats("alice", days=90)
    assert stats["correction_overall"]["flagged"] == 1
    assert stats["correction_overall"]["answers"] == 1
    assert stats["correction_by_model"]["gpt-5"]["flagged"] == 1


def test_compile_stats_fallback_reasons(db_path: Path) -> None:
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)
    _insert_fallback(
        db_path, "alice", "claude-sonnet-5", "budget_refusal", succeeded=False
    )

    stats = self_report.compile_stats("alice", days=7)

    assert stats["fallback_reasons"] == [
        {"reason": "timeout", "count": 2},
        {"reason": "budget_refusal", "count": 1},
    ]


def test_compile_stats_fallback_reasons_is_owner_scoped(db_path: Path) -> None:
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)

    bob_stats = self_report.compile_stats("bob", days=7)
    assert bob_stats["fallback_reasons"] == []


def test_compile_stats_fallback_reasons_reconciles_across_the_retention_boundary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import retention

    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE fallback_log SET created_at = datetime('now', '-60 days')")

    pruned = retention.rollup_and_prune()
    assert pruned["fallback_log"] == 1

    stats = self_report.compile_stats("alice", days=90)
    assert stats["fallback_reasons"] == [{"reason": "timeout", "count": 1}]


def test_compile_stats_is_owner_scoped(db_path: Path) -> None:
    _insert_spend(db_path, "alice", "gpt-5", 1.0)
    _insert_spend(db_path, "bob", "gpt-5", 5.0)

    alice_stats = self_report.compile_stats("alice", days=7)
    bob_stats = self_report.compile_stats("bob", days=7)

    assert alice_stats["spend_usd"] == pytest.approx(1.0)
    assert bob_stats["spend_usd"] == pytest.approx(5.0)


def test_compile_stats_tool_usage_counts(db_path: Path) -> None:
    conv = database.create_conversation("t", "alice")
    database.add_message(
        conv["id"], "assistant", "answer", code_results='[{"code": "1+1"}]'
    )
    database.add_message(
        conv["id"], "assistant", "answer2", sources='[{"url": "https://x"}]'
    )
    database.add_message(conv["id"], "assistant", "plain answer")

    stats = self_report.compile_stats("alice", days=7)

    assert stats["tool_usage"]["code_execution"] == 1
    assert stats["tool_usage"]["web_search"] == 1
    assert stats["tool_usage"]["fact_check"] == 0


# --- render_markdown ----------------------------------------------------------


def test_render_markdown_contains_key_figures(db_path: Path) -> None:
    _insert_spend(db_path, "alice", "gpt-5", 1.23)
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "$1.23" in markdown
    assert "Cache performance" in markdown
    assert "Free-lane routing" in markdown
    assert "Housekeeping" in markdown


def test_render_markdown_fallback_causes_section(db_path: Path) -> None:
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)
    _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=True)
    _insert_fallback(
        db_path, "alice", "gpt-5", "context_length_exceeded", succeeded=True
    )
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "Paid fallback causes" in markdown
    assert "timeout" in markdown
    assert "context-length exceeded" in markdown
    assert "67%" in markdown  # 2/3 timeout
    assert "33%" in markdown  # 1/3 context-length


def test_render_markdown_fallback_causes_section_when_empty(db_path: Path) -> None:
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "No fallbacks this week." in markdown


def test_render_markdown_correction_section_has_the_noisy_proxy_caveat(
    db_path: Path,
) -> None:
    conv = database.create_conversation("t", "alice")
    m1 = database.add_message(
        conv["id"], "assistant", "answer", mode_used="auto->fast:coding", model="gpt-5"
    )
    _insert_correction(
        db_path, "alice", int(m1["id"]), "gpt-5", "auto->fast:coding", "coding"
    )
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "Implicit correction rate" in markdown
    assert "noisy proxy" in markdown
    assert "gpt-5" in markdown
    assert "coding" in markdown


# --- re-run cost section (see app/retry_cost.py) ------------------------------


def _insert_attempt(
    db_path: Path,
    owner: str | None,
    turn_key: int,
    attempt_index: int,
    signal: str | None,
    cost: float,
    mode_used: str = "auto->fast:coding",
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO retry_log (owner, conversation_id, turn_key, "
            "user_message_id, message_id, attempt_index, signal, mode_used, "
            "model, category, tier, cost_usd) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'gpt-5', ?, ?, ?)",
            (
                owner,
                turn_key,
                turn_key,
                turn_key * 100 + attempt_index,
                attempt_index,
                signal,
                mode_used,
                mode_used.split(":", 1)[1] if ":" in mode_used else None,
                mode_used.removeprefix("auto->").split(":", 1)[0],
                cost,
            ),
        )


def test_render_markdown_retry_cost_section_states_what_the_rate_supports(
    db_path: Path,
) -> None:
    _insert_attempt(
        db_path, "alice", turn_key=10, attempt_index=1, signal=None, cost=0.01
    )
    _insert_attempt(
        db_path,
        "alice",
        turn_key=10,
        attempt_index=2,
        signal="regenerated_unrated",
        cost=0.09,
    )
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "Re-run cost (true cost vs first-attempt cost)" in markdown
    # Never a bare percentage: n, the interval, and the sufficiency verdict.
    assert "(1/1 turns)" in markdown
    assert "95% CI" in markdown
    assert "too few to be a finding" in markdown
    assert "$0.01" in markdown and "$0.10" in markdown
    # The ambiguous signal is labelled as ambiguous, not counted as a failure.
    assert "may be taste" in markdown


def test_render_markdown_retry_cost_section_when_there_are_no_reruns(
    db_path: Path,
) -> None:
    stats = self_report.compile_stats("alice", days=7)
    markdown = self_report.render_markdown(stats)

    assert "No re-runs this week." in markdown


# --- generate_report: zero-LLM-by-default / narrate exactly one call ---------


def test_generate_report_zero_llm_by_default(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SELF_REPORT_NARRATE is off by default -- generating a report must
    make NO model call at all."""

    def _fail_if_called() -> object:
        raise AssertionError("no model call should happen with narrate off")

    monkeypatch.setattr(orchestrator, "get_client", _fail_if_called)

    result = self_report.generate_report("alice")

    assert result["narrated"] is False
    messages = database.list_messages(result["conversation_id"])
    assert "# Weekly self-report" in messages[0]["content"]


class _CountingFakeClient:
    def __init__(self, call_count: dict) -> None:
        self._call_count = call_count

    def with_options(self, **kwargs: object) -> "_CountingFakeClient":
        return self

    @property
    def responses(self) -> object:
        outer = self

        class _R:
            def create(self, **kwargs: object) -> object:
                outer._call_count["n"] += 1
                return type("Result", (), {"output_text": "Narrative summary."})()

        return _R()


def test_generate_report_narrate_flag_makes_exactly_one_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SELF_REPORT_NARRATE", "true")
    call_count = {"n": 0}
    monkeypatch.setattr(
        orchestrator, "get_client", lambda: _CountingFakeClient(call_count)
    )

    result = self_report.generate_report("alice")

    assert call_count["n"] == 1
    assert result["narrated"] is True
    messages = database.list_messages(result["conversation_id"])
    assert messages[0]["content"].startswith("Narrative summary.")
    assert "# Weekly self-report" in messages[0]["content"]


def test_generate_report_narrate_flag_on_but_call_fails_still_reports(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narration is best-effort: a failed call must not break report
    generation, and the report still lands with the plain template."""
    monkeypatch.setenv("SELF_REPORT_NARRATE", "true")

    def _raise() -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "get_client", _raise)

    result = self_report.generate_report("alice")

    assert result["narrated"] is False
    messages = database.list_messages(result["conversation_id"])
    assert messages[0]["content"].startswith("# Weekly self-report")


# --- owner scoping of the persisted report -----------------------------------


def test_generate_report_is_owner_scoped(db_path: Path) -> None:
    alice_result = self_report.generate_report("alice")
    bob_result = self_report.generate_report("bob")

    alice_conversations = database.list_conversations("alice")
    bob_conversations = database.list_conversations("bob")

    alice_ids = {c["id"] for c in alice_conversations}
    bob_ids = {c["id"] for c in bob_conversations}

    assert alice_result["conversation_id"] in alice_ids
    assert alice_result["conversation_id"] not in bob_ids
    assert bob_result["conversation_id"] in bob_ids
    assert bob_result["conversation_id"] not in alice_ids

    alice_report = next(
        c for c in alice_conversations if c["id"] == alice_result["conversation_id"]
    )
    assert alice_report["title"].startswith(self_report.REPORT_TITLE_PREFIX)


# --- route wiring --------------------------------------------------------------


def test_conversations_route_with_no_activity_does_not_generate_a_report(
    client: TestClient,
) -> None:
    """The common case every other route-level test relies on implicitly:
    a fresh install's sidebar load must NOT silently add an extra
    conversation to the list."""
    client.get("/v1/conversations")
    conversations = database.list_conversations(None)
    assert conversations == []


def test_conversations_route_generates_a_due_report_via_background_task(
    client: TestClient, db_path: Path
) -> None:
    _insert_spend(db_path, None, "gpt-5", 0.10)
    res = client.get("/v1/conversations")
    assert res.status_code == 200
    conversations = database.list_conversations(None)
    titles = [c["title"] for c in conversations]
    assert any(t.startswith(self_report.REPORT_TITLE_PREFIX) for t in titles)


def test_conversations_route_does_not_duplicate_report_when_not_due(
    client: TestClient, db_path: Path
) -> None:
    _insert_spend(db_path, None, "gpt-5", 0.10)
    client.get("/v1/conversations")
    first_count = len(database.list_conversations(None))
    client.get("/v1/conversations")
    second_count = len(database.list_conversations(None))
    assert first_count == second_count


def test_self_report_status_endpoint(client: TestClient) -> None:
    res = client.get("/v1/self-report/status")
    assert res.status_code == 200
    body = res.json()
    assert body["last_generated_at"] is None
    assert body["narrate_enabled"] is False


def test_self_report_generate_now_endpoint(client: TestClient) -> None:
    res = client.post("/v1/self-report/generate")
    assert res.status_code == 200
    body = res.json()
    assert "conversation_id" in body
    assert body["narrated"] is False

    status = client.get("/v1/self-report/status").json()
    assert status["last_generated_at"] is not None


def test_self_report_generate_now_ignores_staleness(client: TestClient) -> None:
    """The Generate now button always generates, even right after an
    automatic/previous one -- unlike generate_if_due."""
    first = client.post("/v1/self-report/generate").json()
    second = client.post("/v1/self-report/generate").json()
    assert first["conversation_id"] != second["conversation_id"]
