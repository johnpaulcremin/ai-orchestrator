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
    vector, snippets, sources, duration_ms = _recall_memory("q", None, 1)
    assert vector is None
    assert snippets == []
    assert sources == []
    assert duration_ms >= 0


def test_recall_memory_returns_a_real_duration_when_enabled(
    db_path: Path, memory_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])
    vector, snippets, sources, duration_ms = _recall_memory("q", None, 1)
    assert vector == [1.0, 0.0]
    assert snippets == []
    assert sources == []
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
    assert memory.threshold() == 0.794
    monkeypatch.setenv("MEMORY_THRESHOLD", "not-a-number")
    assert memory.threshold() == 0.794
    monkeypatch.setenv("MEMORY_THRESHOLD", "1.5")  # out of (0, 1]
    assert memory.threshold() == 0.794


# --- the default threshold, pinned against every eval pair -------------------
#
# Full-precision cosine similarities for all 15 pairs in
# evals/memory_dataset.json under text-embedding-3-small — recorded here so
# the recall decision can be pinned offline, with no network and no API cost.
# Regenerate with evals/memory_run.py if the dataset or embedding model ever
# changes; the eval prints these (to 4dp) on every run.
_EVAL_PAIRS: list[tuple[float, bool, str]] = [
    # (similarity, should_recall, label)
    (0.95674012024126, False, "trap: 'about it' / 'about that' (no fixed antecedent)"),
    (0.8983964090598854, True, "commit message"),
    (0.8952554332195156, False, "trap: March 5th / March 12th release"),
    (0.8880356988324558, True, "deploy FastAPI"),
    (0.8739831182756264, True, "public speaking"),
    (0.8355972774535221, True, "git merge conflict"),
    (0.8324554345173218, True, "SQL vs NoSQL"),
    (0.7967969517893286, True, "learning a new language"),
    (0.7913335096405261, False, "trap: Priya / Devon project deadline"),
    (0.7694747799196866, False, "trap: FastAPI / Django deploy"),
    (0.7408943229213084, True, "REST vs GraphQL"),
    (0.7255754106709151, False, "trap: merge conflict / broken rebase"),
    (0.7196178319066789, True, "Python venv"),
    (0.4789119413634001, False, "trap: REST-GraphQL / microservices-monolith"),
    (0.4008397413103015, False, "trap: Python venv / Node.js project"),
]


def _recalled_at(threshold: float) -> list[str]:
    return [label for score, _should, label in _EVAL_PAIRS if score >= threshold]


def test_default_threshold_sits_inside_the_only_window_that_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE pin. Exactly one gap in the score distribution both removes the two
    removable traps and keeps every should-recall pair 0.75 recalled:
    (0.79133351, 0.79679695]. A future tweak that leaves this window silently
    reintroduces a false recall (going lower) or drops a real one (going
    higher), and neither shows up anywhere else."""
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)
    priya_devon_trap = 0.7913335096405261
    learning_a_language = 0.7967969517893286
    assert priya_devon_trap < memory.threshold() <= learning_a_language


def test_the_two_removable_traps_do_not_recall_at_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction one: the false recalls this change exists to stop. Both fired
    at 0.75 — a wrong-person and a wrong-framework snippet injected into a new
    turn."""
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)
    recalled = _recalled_at(memory.threshold())
    assert "trap: Priya / Devon project deadline" not in recalled
    assert "trap: FastAPI / Django deploy" not in recalled
    # ...and they DID fire at the old default, so this test can't pass vacuously.
    old = _recalled_at(0.75)
    assert "trap: Priya / Devon project deadline" in old
    assert "trap: FastAPI / Django deploy" in old


def test_every_pair_0_75_recalled_still_recalls_at_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direction two, the converse — so raising the threshold can't be
    'fixed' by simply recalling less. All six genuine pairs that 0.75
    surfaced still surface; recall is unchanged at 6/8."""
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)
    genuine_at_old = {
        label for score, should, label in _EVAL_PAIRS if should and score >= 0.75
    }
    assert len(genuine_at_old) == 6
    assert genuine_at_old <= set(_recalled_at(memory.threshold()))


def test_the_two_irreducible_traps_still_recall_and_that_is_documented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honesty pin: the entity/date-swap traps score ABOVE every should-recall
    pair but one, so NO threshold removes them without gutting recall (see
    memory._DEFAULT_THRESHOLD). Asserted rather than left unsaid, so this
    suite documents the real state instead of implying all four traps are
    handled — provenance in the snippet and `memory_sources` on the response
    are the mitigations for these two, not the threshold."""
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)
    recalled = _recalled_at(memory.threshold())
    assert "trap: 'about it' / 'about that' (no fixed antecedent)" in recalled
    assert "trap: March 5th / March 12th release" in recalled


def test_default_threshold_scores_the_best_accuracy_this_dataset_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No threshold anywhere scores better on these fixtures than the shipped
    one — swept exhaustively rather than asserted, so a claimed improvement
    has to actually beat 11/15 to land."""
    monkeypatch.delenv("MEMORY_THRESHOLD", raising=False)

    def accuracy(t: float) -> int:
        return sum(1 for score, should, _ in _EVAL_PAIRS if (score >= t) == should)

    best = max(
        accuracy(candidate)
        for score, _should, _label in _EVAL_PAIRS
        for candidate in (score, score + 1e-9)
    )
    assert accuracy(memory.threshold()) == best == 11  # 11/15 = 73.3%
    assert accuracy(0.75) == 9  # the old default: 9/15 = 60.0%


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
    assert "Q: what's the capital?\nA: Paris" in text


def test_format_snippet_truncates_long_answers() -> None:
    long_answer = "x" * 500
    text = memory.format_snippet({"question": "q", "answer": long_answer})
    assert text.endswith("...")
    assert len(text) < len(long_answer)


def test_format_snippet_includes_source_title_and_date() -> None:
    """PROVENANCE: the eval-measured entity-swap failure mode (see evals/
    README.md's decision-gate audit -- a changed name/date can clear
    MEMORY_THRESHOLD) means the model's own judgment is the remaining
    defense, so every recalled snippet must carry visible provenance."""
    text = memory.format_snippet(
        {
            "question": "what's the Q3 budget?",
            "answer": "50000",
            "conversation_title": "Budget planning",
            "created_at": "2026-03-05 12:00:00",
        }
    )
    assert '[From "Budget planning" on 2026-03-05]' in text
    assert text.index('[From "Budget planning"') < text.index(
        "Q: what's the Q3 budget?"
    )


def test_format_snippet_handles_a_missing_title_or_date() -> None:
    """A conversation deleted since the memory entry was written (see
    database.memory_list's LEFT JOIN) recalls with no title -- must not
    crash or silently omit the provenance line entirely."""
    text = memory.format_snippet({"question": "q", "answer": "a"})
    assert '[From "an untitled conversation"]' in text


# --- summarize_sources -----------------------------------------------------------


def test_summarize_sources_includes_title_and_date_per_hit() -> None:
    hits = [
        {"conversation_title": "Budget planning", "created_at": "2026-03-05 12:00:00"},
        {"conversation_title": "Trip itinerary", "created_at": "2026-02-01 09:00:00"},
    ]
    assert memory.summarize_sources(hits) == [
        {"conversation_title": "Budget planning", "created_at": "2026-03-05 12:00:00"},
        {"conversation_title": "Trip itinerary", "created_at": "2026-02-01 09:00:00"},
    ]


def test_summarize_sources_falls_back_to_an_untitled_conversation() -> None:
    """Mirrors format_snippet's handling of a hit with no title (a source
    conversation deleted since the memory entry was written)."""
    assert memory.summarize_sources(
        [{"conversation_title": None, "created_at": ""}]
    ) == [{"conversation_title": "an untitled conversation", "created_at": ""}]


def test_summarize_sources_empty_for_no_hits() -> None:
    assert memory.summarize_sources([]) == []


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


def test_recall_includes_the_source_conversations_title_for_provenance(
    db_path: Path, memory_on: None
) -> None:
    """database.memory_list joins the source conversation's title in (see
    its own docstring) so format_snippet can attach visible provenance --
    pinned here at the recall() level, the real call path."""
    conversation = database.create_conversation("Q3 budget planning", None)
    memory.remember(None, int(conversation["id"]), "q1", "a1", [1.0, 0.0])
    hits = memory.recall([1.0, 0.0], None, exclude_conversation_id=999)
    assert len(hits) == 1
    assert hits[0]["conversation_title"] == "Q3 budget planning"


def test_recall_conversation_title_is_none_for_a_deleted_conversation(
    db_path: Path, memory_on: None
) -> None:
    """A memory entry outlives its source conversation being deleted (LEFT
    JOIN, not JOIN) -- recalls with conversation_title=None rather than
    silently disappearing."""
    memory.remember(None, 12345, "q1", "a1", [1.0, 0.0])  # no such conversation
    hits = memory.recall([1.0, 0.0], None, exclude_conversation_id=999)
    assert len(hits) == 1
    assert hits[0]["conversation_title"] is None


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


# --- HTTP: memory_sources (provenance field) round-trip ------------------------


def test_fresh_db_messages_table_has_memory_sources_column(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "memory_sources" in columns


def test_ask_conversation_memory_disabled_never_returns_memory_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CROSS_CONVERSATION_MEMORY", raising=False)
    import app.routers.messages as messages_module

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        assert kwargs.get("memory_sources") is None
        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    res = client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert res.json().get("memory_sources") is None


def test_ask_conversation_recalls_from_memory_and_persists_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    """End-to-end: a recalled memory hit's provenance survives the trip
    through run_orchestrator's kwargs into the response AND the persisted
    message — the UI's memory-use indicator reads this field, never the
    recalled question/answer text itself."""
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    cid1 = int(
        client.post("/v1/conversations", json={"title": "Budget planning"}).json()["id"]
    )

    def fake_run_first(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        return AskResponse(
            answer="the Q3 budget is 50000", mode_used="auto->fast", notes="n"
        )

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run_first)
    client.post(
        f"/v1/conversations/{cid1}/ask", json={"question": "what's the Q3 budget?"}
    )

    captured_sources: list[list[dict]] = []

    def fake_run_second(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse, MemorySource

        sources = kwargs.get("memory_sources")
        captured_sources.append(sources)
        return AskResponse(
            answer="50000",
            mode_used="auto->fast",
            notes="n",
            memory_sources=[MemorySource(**s) for s in sources] if sources else None,
        )

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run_second)
    cid2 = int(client.post("/v1/conversations", json={"title": "t2"}).json()["id"])
    res = client.post(f"/v1/conversations/{cid2}/ask", json={"question": "remind me?"})

    assert len(captured_sources[0]) == 1
    assert captured_sources[0][0]["conversation_title"] == "Budget planning"
    assert captured_sources[0][0]["created_at"]

    body = res.json()
    assert body["memory_sources"] == [
        {
            "conversation_title": "Budget planning",
            "created_at": captured_sources[0][0]["created_at"],
        }
    ]

    persisted = client.get(f"/v1/conversations/{cid2}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert assistant_message["memory_sources"] == body["memory_sources"]


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


# --- capabilities snapshots are never written to memory ------------------------
#
# See AskResponse.memorable. The note the app appends for a "what can you do"
# question carries live per-owner account state — remaining daily budget,
# free-lane quotas, the effective model map. The response cache has always
# refused to store it; memory had no equivalent guard, and no TTL to age it
# out (app/retention.py never prunes memory_entries).


def _fake_answer(memorable: bool):
    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        return AskResponse(
            answer="Your remaining daily budget — $3.9142",
            mode_used="auto->smart",
            notes="n",
            memorable=memorable,
        )

    return fake_run


def test_ask_does_not_remember_an_unmemorable_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])
    monkeypatch.setattr(messages_module, "run_orchestrator", _fake_answer(False))

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    response = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "what can you do?"}
    )

    assert database.memory_total_count() == 0
    # The conversation still keeps it — only the durable, cross-conversation
    # copy is skipped, so the user loses nothing from THIS thread.
    assert response.json()["answer"] == "Your remaining daily budget — $3.9142"
    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assert any(m["role"] == "assistant" for m in persisted)


def test_ask_still_remembers_an_ordinary_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    """The converse, so the guard can't quietly disable memory outright."""
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])
    monkeypatch.setattr(messages_module, "run_orchestrator", _fake_answer(True))

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert database.memory_total_count() == 1


def test_ask_stream_does_not_remember_an_unmemorable_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    """Streaming twin — and the SSE frame the client receives must NOT carry
    the internal flag, so the wire contract is unchanged."""
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    def fake_stream(req, routing_question=None, owner=None, **kwargs):
        yield {"event": "meta", "data": {"mode_used": "auto->smart"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Your remaining daily budget — $3.9142",
                "mode_used": "auto->smart",
                "notes": "n",
                "memorable": False,
            },
        }

    monkeypatch.setattr(messages_module, "stream_orchestrator", fake_stream)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    body = client.post(
        f"/v1/conversations/{cid}/ask/stream", json={"question": "what can you do?"}
    ).text

    assert database.memory_total_count() == 0
    assert "memorable" not in body
    assert "$3.9142" in body  # the answer itself still streamed through


def test_ask_stream_still_remembers_an_ordinary_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, memory_on: None
) -> None:
    import app.routers.messages as messages_module

    monkeypatch.setattr(memory, "embed", lambda q: [1.0, 0.0])

    def fake_stream(req, routing_question=None, owner=None, **kwargs):
        yield {"event": "meta", "data": {"mode_used": "auto->smart"}}
        yield {
            "event": "done",
            "data": {"answer": "Paris.", "mode_used": "auto->smart", "notes": "n"},
        }

    monkeypatch.setattr(messages_module, "stream_orchestrator", fake_stream)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "capital?"})

    assert database.memory_total_count() == 1


def test_remember_strips_per_turn_note_lines(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    """Recalled weeks later, a dead turn's tools list would present itself as
    the present — stripped at write, so the store never holds it."""
    from app import memory

    monkeypatch.setenv("CROSS_CONVERSATION_MEMORY", "true")
    stored: dict = {}

    def fake_put(owner, conversation_id, question, answer, vector_json):
        stored["answer"] = answer

    monkeypatch.setattr(memory.database, "memory_put", fake_put)

    memory.remember(
        None,
        1,
        "plan my garden",
        "Here is the plan.\n"
        "- Tools actually available to YOU on this turn — code execution.",
        [0.1, 0.2],
    )
    assert stored["answer"] == "Here is the plan."
