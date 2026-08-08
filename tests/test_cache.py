from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cache, database, orchestrator
from app.orchestrator import run_orchestrator
from app.schemas import AskRequest, Mode


@pytest.fixture()
def cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")


# --- config / flags ----------------------------------------------------------


def test_enabled_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESPONSE_CACHE", raising=False)
    assert cache.enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "OFF"])
def test_enabled_can_be_disabled(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", value)
    assert cache.enabled() is False


def test_ttl_and_max_entries_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE_TTL_SECONDS", "3600")
    monkeypatch.setenv("RESPONSE_CACHE_MAX_ENTRIES", "50")
    assert cache.ttl_seconds() == 3600
    assert cache.max_entries() == 50
    monkeypatch.setenv("RESPONSE_CACHE_TTL_SECONDS", "nonsense")
    assert cache.ttl_seconds() == 0  # invalid -> disabled


# --- key construction --------------------------------------------------------


def test_make_key_is_stable(db_path: Path) -> None:
    assert cache.make_key("q", "fast") == cache.make_key("q", "fast")


def test_make_key_varies_by_question_and_mode(db_path: Path) -> None:
    assert cache.make_key("q1", "fast") != cache.make_key("q2", "fast")
    assert cache.make_key("q", "fast") != cache.make_key("q", "smart")


def test_make_key_varies_when_config_changes(db_path: Path) -> None:
    before = cache.make_key("q", "fast")
    database.set_setting("OPENAI_MODEL_FAST", "a-different-model")
    after = cache.make_key("q", "fast")
    # A changed model map must yield a different key (auto-invalidation).
    assert before != after


def test_make_key_varies_with_category_env_override(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A category model set via an ENV var (not just the DB) must also change the
    # signature, or an env-configured re-route would serve a stale answer.
    before = cache.make_key("q", "auto")
    monkeypatch.setenv("MODEL_CODING", "claude-opus-4-8")
    after = cache.make_key("q", "auto")
    assert before != after


def test_make_key_varies_by_owner(db_path: Path) -> None:
    # Two different users asking the exact same fresh question must not get
    # back each other's cached answer — a cross-user "has anyone asked X?"
    # oracle, most reachable on a brand-new conversation (no history folded
    # into the question text yet).
    assert cache.make_key("q", "fast", "alice") != cache.make_key("q", "fast", "bob")


def test_make_key_owner_none_is_its_own_distinct_scope(db_path: Path) -> None:
    # The shared/unowned bucket (static-token or auth-disabled mode) must not
    # collide with a real owner's scope, in either direction.
    assert cache.make_key("q", "fast", None) != cache.make_key("q", "fast", "alice")
    assert cache.make_key("q", "fast") == cache.make_key("q", "fast", None)


# --- get / put ---------------------------------------------------------------


def test_put_then_get_round_trip(db_path: Path, cache_on: None) -> None:
    key = cache.make_key("hello", "fast")
    cache.put(key, "hello", "fast", "the answer", "fast", "notes", "m", 3, 4, 0.01)
    hit = cache.get(key)
    assert hit is not None
    assert hit["answer"] == "the answer"
    assert hit["mode_used"] == "fast"


def test_put_skips_empty_answer(db_path: Path, cache_on: None) -> None:
    cache.put("k", "q", "fast", "   ", "fast", "n", "m", 0, 0, 0.0)
    assert database.cache_count() == 0


def test_get_returns_hit_even_if_touch_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    key = cache.make_key("q", "fast")
    cache.put(key, "q", "fast", "ans", "fast", "n", "m", 1, 1, 0.0)

    def boom(_key: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "cache_touch", boom)
    # A failed best-effort touch must not discard an otherwise-valid hit.
    hit = cache.get(key)
    assert hit is not None
    assert hit["answer"] == "ans"


def test_get_and_put_are_noops_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "false")
    cache.put("k", "q", "fast", "answer", "fast", "n", "m", 1, 1, 0.0)
    assert database.cache_count() == 0
    assert cache.get("k") is None


def test_ttl_expiry_evicts_on_read(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    key = "k1"
    database.cache_put(key, "q", "fast", "ans", "fast", "n", "m", 1, 1, 0.0)

    # Backdate the entry so it is older than the TTL.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE response_cache SET created_at = datetime('now', '-3600 seconds') "
        "WHERE key = ?",
        (key,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("RESPONSE_CACHE_TTL_SECONDS", "10")
    assert cache.get(key) is None  # expired
    assert database.cache_get(key) is None  # and deleted


def test_eviction_enforces_max_entries(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("RESPONSE_CACHE_MAX_ENTRIES", "2")
    for i in range(5):
        cache.put(f"k{i}", "q", "fast", f"ans{i}", "fast", "n", "m", 1, 1, 0.0)
    assert database.cache_count() == 2


# --- orchestrator integration ------------------------------------------------


def _stub_model(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def fake_call_model(**kwargs: object) -> str:
        calls.append(str(kwargs["model"]))
        usage = kwargs.get("usage")
        if usage is not None:
            usage.input_tokens = 5  # type: ignore[attr-defined]
            usage.output_tokens = 7  # type: ignore[attr-defined]
        return "answer-42"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)


def test_repeat_prompt_is_served_from_cache(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    first = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert first.answer == "answer-42"
    assert first.cached is False
    assert len(calls) == 1

    second = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.fast))
    assert second.answer == "answer-42"
    assert second.cached is True
    assert second.cost_usd == 0.0
    assert len(calls) == 1  # the model was NOT called again


def test_different_owners_do_not_share_a_cache_hit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    alice = run_orchestrator(
        AskRequest(question="what is 2+2", mode=Mode.fast), owner="alice"
    )
    assert alice.cached is False
    assert len(calls) == 1

    # Bob asks the exact same fresh question — must NOT get Alice's cached
    # answer; the model is called again on his behalf.
    bob = run_orchestrator(
        AskRequest(question="what is 2+2", mode=Mode.fast), owner="bob"
    )
    assert bob.cached is False
    assert len(calls) == 2

    # Alice's own second ask still hits her own cache entry.
    alice_again = run_orchestrator(
        AskRequest(question="what is 2+2", mode=Mode.fast), owner="alice"
    )
    assert alice_again.cached is True
    assert len(calls) == 2


def test_no_cache_flag_bypasses_the_cache(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    run_orchestrator(AskRequest(question="q", mode=Mode.fast))
    fresh = run_orchestrator(AskRequest(question="q", mode=Mode.fast, no_cache=True))
    assert fresh.cached is False
    assert len(calls) == 2  # bypassed the cache, hit the model again


def test_no_cache_does_not_write_to_the_cache(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    _stub_model(monkeypatch, [])

    # no_cache bypasses the cache entirely: it must not populate it either, so a
    # one-off fresh answer (e.g. regenerate) can't poison the shared entry.
    run_orchestrator(AskRequest(question="q", mode=Mode.fast, no_cache=True))
    assert database.cache_count() == 0


def test_changing_the_model_map_invalidates_the_cache(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    run_orchestrator(AskRequest(question="q", mode=Mode.fast))
    assert len(calls) == 1

    # Repoint the fast tier: the signature changes, so the repeat must miss.
    database.set_setting("OPENAI_MODEL_FAST", "other-model")
    run_orchestrator(AskRequest(question="q", mode=Mode.fast))
    assert len(calls) == 2


# --- library generation: a library change must invalidate the cache -----------


@pytest.fixture()
def library_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_LIBRARY", "true")


def _upload(owner: str | None, filename: str) -> None:
    """One document with one chunk, the way app/routers/library.py's upload
    endpoint stores it."""
    import json

    doc = database.library_document_create(owner, filename, "text/plain", 10)
    database.library_chunk_add(
        doc["id"], owner, 0, "the widget costs $10", json.dumps([1.0, 0.0])
    )
    database.library_document_set_chunk_count(doc["id"], 1)


def test_uploading_a_document_invalidates_the_owners_cached_answers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """The staleness this closes: since recall moved inside the orchestrator
    (after routing), the library's contribution is no longer part of the
    question text the key hashes — so without a generation component, a
    byte-identical re-ask in an identical conversation state would be served
    the pre-upload answer for the whole TTL."""
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("RESPONSE_CACHE_TTL_SECONDS", "3600")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    first = run_orchestrator(AskRequest(question="how much is it", mode=Mode.fast))
    assert first.cached is False
    assert len(calls) == 1

    # Same question again, well inside the TTL: served from cache, as designed.
    assert (
        run_orchestrator(AskRequest(question="how much is it", mode=Mode.fast)).cached
        is True
    )
    assert len(calls) == 1

    _upload(None, "manual.txt")

    after_upload = run_orchestrator(
        AskRequest(question="how much is it", mode=Mode.fast)
    )
    assert after_upload.cached is False  # recomputed, not served stale
    assert len(calls) == 2


def test_an_unrelated_question_still_hits_the_cache_when_the_library_is_unchanged(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """The converse, so the generation component can't quietly become "never
    cache": with the library on and populated but UNCHANGED between asks, a
    repeat still hits."""
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("RESPONSE_CACHE_TTL_SECONDS", "3600")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    _upload(None, "manual.txt")

    run_orchestrator(AskRequest(question="something else entirely", mode=Mode.fast))
    assert len(calls) == 1

    repeat = run_orchestrator(
        AskRequest(question="something else entirely", mode=Mode.fast)
    )
    assert repeat.cached is True
    assert len(calls) == 1


def test_deleting_a_document_invalidates_too(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """Removal changes what an answer would be built from just as much as
    addition does — the count half of the fingerprint catches it."""
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    _upload(None, "manual.txt")
    document_id = database.library_documents_list(None)[0]["id"]

    run_orchestrator(AskRequest(question="q", mode=Mode.fast))
    assert len(calls) == 1

    database.library_document_delete(document_id, None)
    assert run_orchestrator(AskRequest(question="q", mode=Mode.fast)).cached is False
    assert len(calls) == 2


def test_library_generation_is_scoped_per_owner(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """Alice uploading must not invalidate Bob's entries."""
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    calls: list[str] = []
    _stub_model(monkeypatch, calls)

    run_orchestrator(AskRequest(question="q", mode=Mode.fast), owner="bob")
    assert len(calls) == 1

    _upload("alice", "alice-only.txt")

    assert (
        run_orchestrator(AskRequest(question="q", mode=Mode.fast), owner="bob").cached
        is True
    )
    assert len(calls) == 1


def test_library_generation_is_empty_and_queries_nothing_when_the_flag_is_off(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-off must be byte-identical to before this existed — including
    issuing no query at all."""
    monkeypatch.delenv("RAG_LIBRARY", raising=False)

    def boom(_owner: str | None) -> tuple[int, int]:
        raise AssertionError("must not query the library when RAG_LIBRARY is off")

    monkeypatch.setattr(database, "library_generation", boom)
    assert cache.library_generation(None) == ""


def test_library_generation_degrades_to_a_never_matching_value_on_a_read_error(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """A failed read must NOT degrade to a value a real library could also
    produce (e.g. "0:0", an empty library) — that would serve exactly the
    stale answer this exists to prevent. A cache miss is the safe direction."""

    def boom(_owner: str | None) -> tuple[int, int]:
        raise sqlite3.OperationalError("no such table: library_chunks")

    monkeypatch.setattr(database, "library_generation", boom)
    degraded = cache.library_generation(None)
    assert degraded == "?"
    assert degraded != "0:0"


def test_semantic_cache_scope_moves_with_the_library_too(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """The semantic cache is MORE exposed to library staleness than the exact
    one (a merely-similar question can hit an entry answered under a different
    library), so its scope key carries the same component."""
    from app.semantic_cache import _scope_key

    before = _scope_key("auto", None)
    _upload(None, "manual.txt")
    assert _scope_key("auto", None) != before


# --- HTTP management ----------------------------------------------------------


def test_cache_status_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    info = client.get("/v1/cache").json()
    assert info["enabled"] is True
    assert info["entries"] == 0


def test_cache_clear_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    database.cache_put("k", "q", "fast", "a", "fast", "n", "m", 1, 1, 0.0)
    assert client.get("/v1/cache").json()["entries"] == 1

    cleared = client.delete("/v1/cache").json()
    assert cleared["cleared"] == 1
    assert cleared["entries"] == 0


def test_cache_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/cache").status_code == 401
    assert client.delete("/v1/cache").status_code == 401
    ok = client.get("/v1/cache", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
