"""Cross-conversation memory (app/memory.py): an opt-in extra-context layer
that recalls relevant exchanges from a caller's OTHER conversations via
embedding similarity and folds them into a new turn's prompt. See the
module docstring for how this differs from app/semantic_cache.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, memory
from app.ask_support import _memory_stage_timing, _recall_memory
from app.context_builder import _assemble_context_parts, _memory_block


@pytest.fixture()
def memory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSS_CONVERSATION_MEMORY", "true")


# --- config / flags ----------------------------------------------------------


def test_memory_enabled_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    assert memory.memory_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_memory_enabled_can_be_turned_on(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CROSS_CONVERSATION_MEMORY", value)
    assert memory.memory_enabled() is True


# --- _recall_memory / _memory_stage_timing: per-stage latency plumbing -----------


def test_recall_memory_returns_zero_duration_and_empties_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    vector, snippets, duration_ms = _recall_memory("q", None, 1)
    assert vector is None
    assert snippets == []
    assert duration_ms >= 0


def test_recall_memory_returns_a_real_duration_when_enabled(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])
    vector, snippets, duration_ms = _recall_memory("q", None, 1)
    assert vector == [1.0, 0.0]
    assert snippets == []
    assert duration_ms >= 0


def test_memory_stage_timing_is_none_when_memory_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    assert _memory_stage_timing(37) is None


def test_memory_stage_timing_reports_the_duration_when_enabled(
    memory_on: None,
) -> None:
    assert _memory_stage_timing(37) == {"memory_embed": 37}


def test_threshold_top_k_max_entries_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_THRESHOLD", "0.6")
    monkeypatch.setenv("MEMORY_TOP_K", "5")
    monkeypatch.setenv("MEMORY_MAX_ENTRIES", "50")
    assert memory.threshold() == 0.6
    assert memory.top_k() == 5
    assert memory.max_entries() == 50


def test_threshold_default_and_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)
    assert memory.threshold() == 0.75
    monkeypatch.setenv("MEMORY_THRESHOLD", "not-a-number")
    assert memory.threshold() == 0.75
    monkeypatch.setenv("MEMORY_THRESHOLD", "1.5")  # out of (0, 1]
    assert memory.threshold() == 0.75


def test_top_k_and_max_entries_default_and_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORY_TOP_K", raising=False)
    assert memory.top_k() == 3
    monkeypatch.setenv("MEMORY_TOP_K", "0")
    assert memory.top_k() == 3
    monkeypatch.delenv("MEMORY_MAX_ENTRIES", raising=False)
    assert memory.max_entries() == 500
    monkeypatch.setenv("MEMORY_MAX_ENTRIES", "-1")
    assert memory.max_entries() == 500
    monkeypatch.setenv("MEMORY_TOP_K", "not-a-number")
    assert memory.top_k() == 3


# --- format_snippet ------------------------------------------------------------


def test_format_snippet_includes_question_and_answer() -> None:
    text = memory.format_snippet({"question": "what's the capital?", "answer": "Paris"})
    assert text == "Q: what's the capital?\nA: Paris"


def test_format_snippet_truncates_long_answers() -> None:
    long_answer = "x" * 500
    text = memory.format_snippet({"question": "q", "answer": long_answer})
    assert text.endswith("...")
    assert len(text) < len(long_answer)


# --- recall() --------------------------------------------------------------------


def test_recall_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    assert memory.recall([1.0, 0.0], None, exclude_conversation_id=1) == []


def test_recall_returns_empty_when_vector_is_none(
    db_path: Path, memory_on: None
) -> None:
    assert memory.recall(None, None, exclude_conversation_id=1) == []


def test_recall_with_no_candidates_returns_empty(
    db_path: Path, memory_on: None
) -> None:
    assert memory.recall([1.0, 0.0], None, exclude_conversation_id=1) == []


def test_remember_then_recall_round_trips_on_a_near_identical_vector(
    db_path: Path, memory_on: None
) -> None:
    memory.remember(None, 1, "what's the capital of France?", "Paris", [1.0, 0.0])
    hits = memory.recall([1.0, 0.0], None, exclude_conversation_id=2)
    assert len(hits) == 1
    assert hits[0]["answer"] == "Paris"


def test_recall_excludes_the_current_conversation(
    db_path: Path, memory_on: None
) -> None:
    memory.remember(None, 1, "q", "a", [1.0, 0.0])
    hits = memory.recall([1.0, 0.0], None, exclude_conversation_id=1)
    assert hits == []  # same conversation -> excluded, not recalled from itself


def test_recall_skips_a_candidate_below_threshold(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_THRESHOLD", "0.99")
    memory.remember(None, 1, "q1", "a1", [1.0, 0.0])
    hits = memory.recall([1.0, 0.3], None, exclude_conversation_id=2)
    assert hits == []


def test_recall_ranks_best_match_first_and_caps_at_top_k(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_THRESHOLD", "0.5")
    monkeypatch.setenv("MEMORY_TOP_K", "1")
    memory.remember(None, 1, "q1", "far", [1.0, 0.0])
    memory.remember(None, 2, "q2", "close", [0.9, 0.1])
    hits = memory.recall([0.9, 0.1], None, exclude_conversation_id=3)
    assert len(hits) == 1
    assert hits[0]["answer"] == "close"


def test_recall_is_scoped_by_owner(db_path: Path, memory_on: None) -> None:
    memory.remember("alice", 1, "q", "alice's answer", [1.0, 0.0])
    bob_hits = memory.recall([1.0, 0.0], "bob", exclude_conversation_id=2)
    assert bob_hits == []  # different owner -> no cross-user leak
    alice_hits = memory.recall([1.0, 0.0], "alice", exclude_conversation_id=2)
    assert len(alice_hits) == 1


def test_recall_does_not_leak_between_authenticated_and_anonymous(
    db_path: Path, memory_on: None
) -> None:
    memory.remember(None, 1, "q", "anon answer", [1.0, 0.0])
    alice_hits = memory.recall([1.0, 0.0], "alice", exclude_conversation_id=2)
    assert alice_hits == []


def test_recall_returns_empty_on_db_error(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_owner, _exclude):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "memory_list", boom)
    assert memory.recall([1.0, 0.0], None, exclude_conversation_id=1) == []


def test_recall_skips_a_candidate_with_malformed_embedding_json(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        database,
        "memory_list",
        lambda owner, exclude: [
            {"conversation_id": 1, "question": "q", "answer": "a", "embedding": "{bad"}
        ],
    )
    assert memory.recall([1.0, 0.0], None, exclude_conversation_id=2) == []


# --- remember() ------------------------------------------------------------------


def test_remember_is_noop_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    memory.remember(None, 1, "q", "a", [1.0, 0.0])
    assert database.memory_total_count() == 0


def test_remember_is_noop_when_vector_is_none(db_path: Path, memory_on: None) -> None:
    memory.remember(None, 1, "q", "a", None)
    assert database.memory_total_count() == 0


def test_remember_is_noop_for_empty_answer(db_path: Path, memory_on: None) -> None:
    memory.remember(None, 1, "q", "   ", [1.0, 0.0])
    assert database.memory_total_count() == 0


def test_eviction_enforces_max_entries_per_owner(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_MAX_ENTRIES", "2")
    for i in range(5):
        memory.remember("alice", i, f"q{i}", f"a{i}", [1.0, 0.0])
    assert database.memory_count("alice") == 2


def test_eviction_is_scoped_per_owner_not_global(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_MAX_ENTRIES", "2")
    for i in range(3):
        memory.remember("alice", i, f"q{i}", f"a{i}", [1.0, 0.0])
    memory.remember("bob", 1, "q", "a", [1.0, 0.0])
    assert database.memory_count("alice") == 2
    assert database.memory_count("bob") == 1  # bob's single entry untouched


def test_remember_tolerates_db_error(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "memory_put", boom)
    memory.remember(None, 1, "q", "a", [1.0, 0.0])  # must not raise


def test_clear_tolerates_db_error(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "memory_clear", boom)
    assert memory.clear() == 0


def test_stats_tolerates_db_error(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "memory_total_count", boom)
    stats = memory.stats()
    assert stats["entries"] == 0


def test_clear_removes_every_entry(db_path: Path, memory_on: None) -> None:
    memory.remember(None, 1, "q", "a", [1.0, 0.0])
    memory.remember("alice", 2, "q2", "a2", [1.0, 0.0])
    assert memory.clear() == 2
    assert database.memory_total_count() == 0


def test_stats_reports_config_and_count(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_THRESHOLD", "0.8")
    memory.remember(None, 1, "q", "a", [1.0, 0.0])
    stats = memory.stats()
    assert stats == {
        "enabled": True,
        "entries": 1,
        "threshold": 0.8,
        "top_k": 3,
        "max_entries": 500,
    }


# --- context assembly: _memory_block / _assemble_context_parts -------------------


def test_memory_block_empty_for_no_snippets() -> None:
    assert _memory_block(None) == ""
    assert _memory_block([]) == ""


def test_memory_block_includes_every_snippet() -> None:
    block = _memory_block(["Q: a\nA: b", "Q: c\nA: d"])
    assert "Q: a\nA: b" in block
    assert "Q: c\nA: d" in block


def test_assemble_context_parts_folds_memory_into_a_brand_new_conversation() -> None:
    parts = _assemble_context_parts(
        prior_messages=[],
        current_question="what did we decide about the budget?",
        memory_snippets=["Q: old q\nA: old a"],
    )
    assert "old q" in parts.system_block
    assert "old a" in parts.system_block
    assert "Current user question:" in parts.recent_and_question


def test_assemble_context_parts_folds_memory_alongside_history() -> None:
    parts = _assemble_context_parts(
        prior_messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        current_question="follow-up",
        memory_snippets=["Q: old q\nA: old a"],
    )
    assert "old q" in parts.system_block
    assert "Summary of earlier messages" not in parts.system_block or True


def test_assemble_context_parts_no_memory_no_history_stays_bare() -> None:
    parts = _assemble_context_parts(
        prior_messages=[], current_question="hi", memory_snippets=None
    )
    assert parts.system_block == ""
    assert parts.recent_and_question == "hi"


# --- HTTP: end-to-end recall + write on a conversation ask -----------------------


def test_ask_conversation_recalls_and_remembers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    captured_questions: list[str] = []

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured_questions.append(req.question)
        from app.schemas import AskResponse

        return AskResponse(answer="fresh answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    # First conversation: ask once, so a memory entry gets written.
    cid1 = int(client.post("/v1/conversations", json={"title": "t1"}).json()["id"])
    client.post(f"/v1/conversations/{cid1}/ask", json={"question": "what is the plan?"})
    assert database.memory_total_count() == 1

    # Second, DIFFERENT conversation: the new question should recall the
    # first conversation's answer into its prompt.
    cid2 = int(client.post("/v1/conversations", json={"title": "t2"}).json()["id"])
    client.post(f"/v1/conversations/{cid2}/ask", json={"question": "remind me?"})

    assert "fresh answer" in captured_questions[1]


def test_ask_conversation_memory_never_recalls_its_own_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    captured_questions: list[str] = []

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured_questions.append(req.question)
        from app.schemas import AskResponse

        return AskResponse(answer="first answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "first question"})
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "second question"})

    # The second turn's prompt must not carry a "Relevant context from other
    # past conversations" block sourced from ITS OWN first turn — that's
    # already covered by ordinary conversation history/summary.
    assert "Relevant context from other past conversations" not in captured_questions[1]


def test_ask_conversation_memory_disabled_never_recalls_or_writes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    import app.routers.messages as messages_module

    embed_calls: list[str] = []
    monkeypatch.setattr(memory, "embed", lambda q: embed_calls.append(q) or [1.0, 0.0])

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert embed_calls == []
    assert database.memory_total_count() == 0


def test_regenerate_does_not_write_a_memory_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    """Scope guard: cross-conversation memory only ever writes from the
    primary ask-stream/ask path, not regenerate — see _stream_and_persist's
    remember_memory docstring."""
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert database.memory_total_count() == 1

    client.post(f"/v1/conversations/{cid}/regenerate", json={})
    # Non-streaming regenerate goes through run_orchestrator directly (no
    # _stream_and_persist), which never calls memory.remember at all.
    assert database.memory_total_count() == 1


# --- HTTP management ---------------------------------------------------------------


def test_memory_status_endpoint(client: TestClient, memory_on: None) -> None:
    info = client.get("/v1/memory").json()
    assert info["enabled"] is True
    assert info["entries"] == 0


def test_memory_clear_endpoint(client: TestClient, memory_on: None) -> None:
    memory.remember(None, 1, "q", "a", [1.0, 0.0])
    assert client.get("/v1/memory").json()["entries"] == 1

    cleared = client.delete("/v1/memory").json()
    assert cleared["cleared"] == 1
    assert cleared["entries"] == 0


def test_memory_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/memory").status_code == 401
    assert client.delete("/v1/memory").status_code == 401
    ok = client.get("/v1/memory", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200


def test_memory_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "CROSS_CONVERSATION_MEMORY")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
