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
    def fake_call_model(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage: object | None = None,
    ) -> str:
        calls.append(model)
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
