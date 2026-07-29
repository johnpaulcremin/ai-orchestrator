"""Semantic (paraphrase) response caching (app/semantic_cache.py): an
opt-in, high-threshold, CONTEXT-FREE-ONLY layer on top of the exact-match
response cache. See the module docstring for the two correctness guardrails
this suite exists to lock in.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, orchestrator, orchestrator_calls, semantic_cache
from app.orchestrator import run_orchestrator
from app.schemas import AskRequest, Mode


@pytest.fixture()
def semantic_cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE", "true")


# --- config / flags ------------------------------------------------------------


def test_enabled_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEMANTIC_CACHE", raising=False)
    assert semantic_cache.enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_enabled_can_be_turned_on(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE", value)
    assert semantic_cache.enabled() is True


def test_threshold_and_max_entries_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.9")
    monkeypatch.setenv("SEMANTIC_CACHE_MAX_ENTRIES", "50")
    assert semantic_cache.threshold() == 0.9
    assert semantic_cache.max_entries() == 50


def test_threshold_default_and_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEMANTIC_CACHE_THRESHOLD", raising=False)
    assert semantic_cache.threshold() == 0.96
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "not-a-number")
    assert semantic_cache.threshold() == 0.96
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "1.5")  # out of (0, 1]
    assert semantic_cache.threshold() == 0.96


# --- cosine similarity ----------------------------------------------------------


def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert semantic_cache._cosine_similarity(
        [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]
    ) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert semantic_cache._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(
        0.0
    )


def test_cosine_similarity_opposite_vectors_is_negative_one() -> None:
    assert semantic_cache._cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(
        -1.0
    )


def test_cosine_similarity_mismatched_lengths_is_zero() -> None:
    assert semantic_cache._cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_cosine_similarity_zero_vector_is_zero() -> None:
    assert semantic_cache._cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# --- embed(): fail-safe -----------------------------------------------------------


def test_embed_returns_none_for_blank_text() -> None:
    assert semantic_cache.embed("   ") is None


def test_embed_returns_none_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(orchestrator_calls, "_client", None)
    assert semantic_cache.embed("hello") is None


def test_embed_returns_none_on_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomClient:
        def with_options(self, **kwargs):
            return self

        class embeddings:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "get_client", lambda: BoomClient())
    assert semantic_cache.embed("hello") is None


def test_embed_returns_the_vector_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    class FakeClient:
        def with_options(self, **kwargs):
            return self

        class embeddings:
            @staticmethod
            def create(**kwargs):
                data = [types.SimpleNamespace(embedding=[0.1, 0.2, 0.3])]
                return types.SimpleNamespace(data=data)

    monkeypatch.setattr(orchestrator, "get_client", lambda: FakeClient())
    assert semantic_cache.embed("hello") == [0.1, 0.2, 0.3]


# --- embed(): the shared embedding cache ------------------------------------------


def _counting_client(monkeypatch: pytest.MonkeyPatch, vector: list[float]) -> list[str]:
    """Patches orchestrator.get_client with a fake that records every text it
    was asked to embed, returning `vector` each time."""
    import types

    calls: list[str] = []

    class FakeClient:
        def with_options(self, **kwargs):
            return self

        class embeddings:
            @staticmethod
            def create(**kwargs):
                calls.append(str(kwargs["input"]))
                data = [types.SimpleNamespace(embedding=list(vector))]
                return types.SimpleNamespace(data=data)

    monkeypatch.setattr(orchestrator, "get_client", lambda: FakeClient())
    return calls


def test_embed_caches_identical_text_across_calls(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_client(monkeypatch, [0.1, 0.2])
    assert semantic_cache.embed("what's the capital of France?") == [0.1, 0.2]
    assert semantic_cache.embed("what's the capital of France?") == [0.1, 0.2]
    assert len(calls) == 1  # second call served from the cache, no API call


def test_embed_does_not_cache_across_different_text(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_client(monkeypatch, [0.1, 0.2])
    semantic_cache.embed("question one")
    semantic_cache.embed("question two")
    assert len(calls) == 2


def test_embed_does_not_cache_across_different_models(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_client(monkeypatch, [0.1, 0.2])
    monkeypatch.delenv("SEMANTIC_CACHE_EMBEDDING_MODEL", raising=False)
    semantic_cache.embed("hello")
    monkeypatch.setenv("SEMANTIC_CACHE_EMBEDDING_MODEL", "text-embedding-3-large")
    semantic_cache.embed("hello")
    assert len(calls) == 2  # different (model, text) key -> both are cache misses


def test_embed_cache_survives_a_corrupt_stored_row(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import database

    calls = _counting_client(monkeypatch, [0.1, 0.2])
    key = semantic_cache._embedding_cache_key(
        semantic_cache._embedding_model(), "hello"
    )
    database.embedding_cache_put(key, "not valid json")
    assert semantic_cache.embed("hello") == [0.1, 0.2]
    assert len(calls) == 1  # corrupt row was ignored, not treated as a hit


def test_embed_tolerates_a_cache_read_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import database

    def boom(_key: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "embedding_cache_get", boom)
    calls = _counting_client(monkeypatch, [0.1, 0.2])
    assert semantic_cache.embed("hello") == [0.1, 0.2]
    assert len(calls) == 1


def test_embed_tolerates_a_cache_write_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import database

    def boom(_key: str, _embedding: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "embedding_cache_put", boom)
    calls = _counting_client(monkeypatch, [0.1, 0.2])
    # A failed cache write must not fail embed() itself.
    assert semantic_cache.embed("hello") == [0.1, 0.2]
    assert len(calls) == 1


def test_embed_cache_eviction_enforces_max_entries(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import database

    monkeypatch.setattr(semantic_cache, "_EMBEDDING_CACHE_MAX_ENTRIES", 2)
    _counting_client(monkeypatch, [0.1, 0.2])
    for i in range(5):
        semantic_cache.embed(f"question {i}")
    assert database.embedding_cache_count() == 2


# --- find() / put(): gating and matching ------------------------------------------


def test_find_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEMANTIC_CACHE", raising=False)
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    hit, vector = semantic_cache.find("q", "fast", None)
    assert hit is None
    assert vector is None


def test_find_returns_none_when_embed_fails(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: None)
    hit, vector = semantic_cache.find("q", "fast", None)
    assert hit is None
    assert vector is None


def test_find_with_no_candidates_returns_none(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    hit, vector = semantic_cache.find("q", "fast", None)
    assert hit is None
    assert vector == [1.0, 0.0]


def test_put_then_find_round_trips_on_a_near_identical_vector(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    semantic_cache.put(
        "what's the capital of France?",
        "fast",
        None,
        [1.0, 0.0],
        "Paris",
        "auto->fast",
        "n",
        "gpt-5-mini",
        5,
        3,
        0.001,
    )
    hit, vector = semantic_cache.find("capital of france", "fast", None)
    assert hit is not None
    assert hit["answer"] == "Paris"
    assert hit["similarity"] == pytest.approx(1.0)


def test_find_skips_a_candidate_below_threshold(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.99")
    # Stored vector and query vector are related but not near-identical.
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.3])
    semantic_cache.put(
        "q1", "fast", None, [1.0, 0.0], "a1", "auto->fast", "n", "m", 1, 1, 0.0
    )
    hit, _vector = semantic_cache.find("q2", "fast", None)
    assert hit is None


def test_find_picks_the_best_of_several_candidates(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.5")
    semantic_cache.put(
        "q1", "fast", None, [1.0, 0.0], "far", "auto->fast", "n", "m", 1, 1, 0.0
    )
    semantic_cache.put(
        "q2", "fast", None, [0.9, 0.1], "close", "auto->fast", "n", "m", 1, 1, 0.0
    )
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [0.9, 0.1])
    hit, _vector = semantic_cache.find("q3", "fast", None)
    assert hit is not None
    assert hit["answer"] == "close"


def test_find_is_scoped_by_mode(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    semantic_cache.put(
        "q", "fast", None, [1.0, 0.0], "fast-answer", "auto->fast", "n", "m", 1, 1, 0.0
    )
    hit, _vector = semantic_cache.find("q", "smart", None)
    assert hit is None  # different mode -> different scope, no match


def test_find_is_scoped_by_owner(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    semantic_cache.put(
        "q", "fast", "alice", [1.0, 0.0], "a1", "auto->fast", "n", "m", 1, 1, 0.0
    )
    hit, _vector = semantic_cache.find("q", "fast", "bob")
    assert hit is None  # different owner -> no cross-user leak
    alice_hit, _vector2 = semantic_cache.find("q", "fast", "alice")
    assert alice_hit is not None


def test_put_is_noop_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEMANTIC_CACHE", raising=False)
    semantic_cache.put(
        "q", "fast", None, [1.0, 0.0], "a", "auto->fast", "n", "m", 1, 1, 0.0
    )
    assert database.semantic_cache_count() == 0


def test_put_is_noop_when_vector_is_none(
    db_path: Path, semantic_cache_on: None
) -> None:
    semantic_cache.put("q", "fast", None, None, "a", "auto->fast", "n", "m", 1, 1, 0.0)
    assert database.semantic_cache_count() == 0


def test_put_is_noop_for_empty_answer(db_path: Path, semantic_cache_on: None) -> None:
    semantic_cache.put(
        "q", "fast", None, [1.0, 0.0], "   ", "auto->fast", "n", "m", 1, 1, 0.0
    )
    assert database.semantic_cache_count() == 0


def test_eviction_enforces_max_entries(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE_MAX_ENTRIES", "2")
    for i in range(5):
        semantic_cache.put(
            f"q{i}",
            "fast",
            None,
            [1.0, 0.0],
            f"a{i}",
            "auto->fast",
            "n",
            "m",
            1,
            1,
            0.0,
        )
    assert database.semantic_cache_count() == 2


def test_clear_removes_every_entry(db_path: Path, semantic_cache_on: None) -> None:
    semantic_cache.put(
        "q", "fast", None, [1.0, 0.0], "a", "auto->fast", "n", "m", 1, 1, 0.0
    )
    assert semantic_cache.clear() == 1
    assert database.semantic_cache_count() == 0


def test_stats_reports_config_and_count(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.9")
    semantic_cache.put(
        "q", "fast", None, [1.0, 0.0], "a", "auto->fast", "n", "m", 1, 1, 0.0
    )
    stats = semantic_cache.stats()
    assert stats == {
        "enabled": True,
        "entries": 1,
        "threshold": 0.9,
        "max_entries": 200,
    }


# --- orchestrator integration: the context_free gate ----------------------------


def _stub_model(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_call_model(**kwargs: object) -> str:
        calls.append(str(kwargs["model"]))
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 5  # type: ignore[attr-defined]
            usage.output_tokens = 7  # type: ignore[attr-defined]
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)


def test_context_free_true_writes_a_semantic_entry_on_a_miss(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    _stub_model(monkeypatch, [])

    result = run_orchestrator(
        AskRequest(question="what is 2+2", mode=Mode.fast), context_free=True
    )
    assert result.answer == "the answer"
    assert database.semantic_cache_count() == 1


def test_context_free_false_never_writes_a_semantic_entry(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critical safety gate: even with semantic caching ON, a
    context-bearing question (conversation history/instructions folded in)
    must never be embedded or written to the semantic index."""
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    embed_calls: list[str] = []
    monkeypatch.setattr(
        semantic_cache,
        "embed",
        lambda q: embed_calls.append(q) or [1.0, 0.0],
    )
    _stub_model(monkeypatch, [])

    run_orchestrator(
        AskRequest(question="what is 2+2", mode=Mode.fast), context_free=False
    )
    assert embed_calls == []  # no embedding call was ever made
    assert database.semantic_cache_count() == 0


def test_context_free_true_serves_a_paraphrase_on_the_next_ask(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    monkeypatch.setenv("SEMANTIC_CACHE_THRESHOLD", "0.9")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    vectors = {"what's 2 plus 2?": [1.0, 0.0], "2+2=?": [0.99, 0.05]}
    monkeypatch.setattr(semantic_cache, "embed", lambda q: vectors[q])

    first = run_orchestrator(
        AskRequest(question="what's 2 plus 2?", mode=Mode.fast), context_free=True
    )
    assert first.cached is False
    assert len(calls) == 1

    second = run_orchestrator(
        AskRequest(question="2+2=?", mode=Mode.fast), context_free=True
    )
    assert second.answer == "the answer"
    assert second.cached is True
    assert second.cost_usd == 0.0
    assert len(calls) == 1  # served from the semantic cache, model not called again
    assert "semantic cache" in second.notes.lower()
    assert "similarity=" in second.notes


def test_semantic_hit_records_avoided_cost(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    semantic_cache.put(
        "q",
        "fast",
        None,
        [1.0, 0.0],
        "a",
        "auto->fast",
        "n",
        "fast-model",
        5,
        5,
        0.02,
    )
    run_orchestrator(AskRequest(question="q2", mode=Mode.fast), context_free=True)
    assert database.avoided_cost_today(None) == pytest.approx(0.02)


def test_semantic_cache_never_consulted_when_exact_cache_already_hits(
    db_path: Path, semantic_cache_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact-match hit must short-circuit before the semantic lookup even
    runs — no embedding call for a byte-identical repeat."""
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    _stub_model(monkeypatch, [])

    run_orchestrator(AskRequest(question="q", mode=Mode.fast), context_free=True)

    embed_calls: list[str] = []
    monkeypatch.setattr(
        semantic_cache, "embed", lambda q: embed_calls.append(q) or [1.0, 0.0]
    )
    second = run_orchestrator(
        AskRequest(question="q", mode=Mode.fast), context_free=True
    )
    assert second.cached is True
    assert embed_calls == []


def test_semantic_cache_disabled_is_never_consulted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEMANTIC_CACHE", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    embed_calls: list[str] = []
    monkeypatch.setattr(
        semantic_cache, "embed", lambda q: embed_calls.append(q) or [1.0, 0.0]
    )
    _stub_model(monkeypatch, [])

    run_orchestrator(AskRequest(question="q", mode=Mode.fast), context_free=True)
    assert embed_calls == []
    assert database.semantic_cache_count() == 0


# --- main.py: _is_context_free -------------------------------------------------


def test_ask_conversation_first_message_is_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages

    captured: dict[str, object] = {}

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured.update(kwargs)
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert captured["context_free"] is True


def test_ask_conversation_with_prior_history_is_not_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages
    from app.database import add_message

    captured: dict[str, object] = {}

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured.update(kwargs)
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    add_message(conversation_id=cid, role="user", content="earlier turn")
    add_message(conversation_id=cid, role="assistant", content="earlier answer")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "follow-up"})
    assert captured["context_free"] is False


def test_ask_conversation_with_system_prompt_is_not_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages

    captured: dict[str, object] = {}

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured.update(kwargs)
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.put(
        f"/v1/conversations/{cid}/system_prompt",
        json={"system_prompt": "Always answer in French."},
    )
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert captured["context_free"] is False


def test_bare_ask_endpoint_is_always_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.ask

    captured: dict[str, object] = {}

    def fake_run(req, owner=None, **kwargs):
        captured.update(kwargs)
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.ask, "run_orchestrator", fake_run)

    client.post("/v1/ask", json={"question": "hi"})
    assert captured["context_free"] is True


def test_edit_message_does_not_default_to_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: edit doesn't force no_cache, so the context_free
    default (False) is the only thing stopping a context-bearing edit from
    reaching the semantic cache."""
    import app.routers.messages

    captured: dict[str, object] = {}

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        captured.update(kwargs)
        from app.schemas import AskResponse

        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    message_id = None
    for m in client.get(f"/v1/conversations/{cid}/messages").json():
        if m["role"] == "user":
            message_id = m["id"]
    assert message_id is not None

    captured.clear()  # discard the first ask's own (context-free) call
    edit_res = client.post(
        f"/v1/conversations/{cid}/messages/{message_id}/edit",
        json={"question": "hi again"},
    )
    assert edit_res.status_code == 200
    assert captured.get("context_free", False) is False


# --- HTTP management -------------------------------------------------------------


def test_semantic_cache_status_endpoint(
    client: TestClient, semantic_cache_on: None
) -> None:
    info = client.get("/v1/semantic-cache").json()
    assert info["enabled"] is True
    assert info["entries"] == 0


def test_semantic_cache_clear_endpoint(
    client: TestClient, semantic_cache_on: None
) -> None:
    database.semantic_cache_put(
        "scope", "q", "[1.0, 0.0]", "a", "auto->fast", "n", "m", 1, 1, 0.0
    )
    assert client.get("/v1/semantic-cache").json()["entries"] == 1

    cleared = client.delete("/v1/semantic-cache").json()
    assert cleared["cleared"] == 1
    assert cleared["entries"] == 0


def test_semantic_cache_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/semantic-cache").status_code == 401
    assert client.delete("/v1/semantic-cache").status_code == 401
    ok = client.get(
        "/v1/semantic-cache", headers={"Authorization": "Bearer secret-token"}
    )
    assert ok.status_code == 200


def test_semantic_cache_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "SEMANTIC_CACHE")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False


# --- database layer ---------------------------------------------------------------


def test_semantic_cache_get_returns_empty_list_on_db_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_scope: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "semantic_cache_list", boom)
    monkeypatch.setenv("SEMANTIC_CACHE", "true")
    monkeypatch.setattr(semantic_cache, "embed", lambda q: [1.0, 0.0])
    hit, vector = semantic_cache.find("q", "fast", None)
    assert hit is None
    assert vector == [1.0, 0.0]
