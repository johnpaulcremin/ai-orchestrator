"""Web search retrieval: the router's needs_live_data signal, its final gating
(WEB_SEARCH opt-in + OpenAI-only), the response cache skip for freshness-
sensitive answers, and end-to-end citation persistence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import cache
from app.database import add_message, list_messages
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.routing import (
    _gate_live_data,
    _looks_time_sensitive_fallback,
    _parse_classifier_json,
    _web_search_enabled,
    decide_route,
)
from app.schemas import AskRequest, Mode


def _classifier(
    category: str, complexity: str, needs_live_data: bool = False
) -> object:
    """A fake OpenAI client whose classifier returns this classification."""
    text = json.dumps(
        {
            "category": category,
            "complexity": complexity,
            "reason": "t",
            "needs_live_data": needs_live_data,
        }
    )
    result = SimpleNamespace(output_text=text)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kw: result))
    client.with_options = lambda **_kw: client  # type: ignore[attr-defined]
    return client


# --- _web_search_enabled -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("true", True),
        ("1", True),
    ],
)
def test_web_search_enabled_parsing(
    raw: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is None:
        monkeypatch.delenv("WEB_SEARCH", raising=False)
    else:
        monkeypatch.setenv("WEB_SEARCH", raw)
    assert _web_search_enabled() is expected


# --- _gate_live_data ----------------------------------------------------------


def test_gate_live_data_requires_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    assert _gate_live_data(True, "gpt-5") is True  # wants + enabled + openai
    assert _gate_live_data(False, "gpt-5") is False  # didn't want it
    assert _gate_live_data(True, "claude-sonnet-5") is False  # not OpenAI-served
    assert _gate_live_data(True, "gemini/gemini-flash-latest") is False  # LiteLLM


def test_gate_live_data_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH", raising=False)
    assert _gate_live_data(True, "gpt-5") is False


# --- heuristic fallback phrase matching ---------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what's the weather today",
        "What is today's weather in Paris?",
        "who won the game last night",
        "what's the current stock price of AAPL",
        "give me the latest news on the merger",
        "what's the exchange rate for USD to EUR",
    ],
)
def test_looks_time_sensitive_fallback_positive(question: str) -> None:
    assert _looks_time_sensitive_fallback(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "what does the current function do",
        "explain the latest commit",
        "now let's add tests for this",
        "review my current implementation",
        "what is 2+2",
        "write a Python function to sort a list",
    ],
)
def test_looks_time_sensitive_fallback_negative(question: str) -> None:
    # Ordinary dev/coding questions must NOT trigger a paid search.
    assert _looks_time_sensitive_fallback(question) is False


# --- classifier JSON parsing of needs_live_data -------------------------------


def test_parse_classifier_json_needs_live_data_bool() -> None:
    raw = '{"category": "quick_fact", "complexity": "low", "reason": "r", "needs_live_data": true}'
    parsed = _parse_classifier_json(raw)
    assert parsed is not None
    assert parsed["needs_live_data"] is True


def test_parse_classifier_json_needs_live_data_missing_defaults_false() -> None:
    raw = '{"category": "quick_fact", "complexity": "low", "reason": "r"}'
    parsed = _parse_classifier_json(raw)
    assert parsed is not None
    assert parsed["needs_live_data"] is False


def test_parse_classifier_json_needs_live_data_string_coerced() -> None:
    raw = '{"category": "quick_fact", "complexity": "low", "reason": "r", "needs_live_data": "true"}'
    parsed = _parse_classifier_json(raw)
    assert parsed is not None
    assert parsed["needs_live_data"] is True


# --- decide_route end-to-end gating -------------------------------------------


def test_decide_route_enables_web_search_when_classifier_flags_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
    client = _classifier("quick_fact", "medium", needs_live_data=True)
    decision = decide_route("what's the weather today", Mode.auto, client=client)
    assert decision.needs_live_data is True


def test_decide_route_web_search_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB_SEARCH", raising=False)
    client = _classifier("quick_fact", "medium", needs_live_data=True)
    decision = decide_route("what's the weather today", Mode.auto, client=client)
    assert decision.needs_live_data is False


def test_decide_route_web_search_false_when_classifier_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    client = _classifier("coding", "medium", needs_live_data=False)
    decision = decide_route("what does the current file do", Mode.auto, client=client)
    assert decision.needs_live_data is False


def test_decide_route_web_search_false_for_non_openai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    monkeypatch.setenv("MODEL_QUICK_FACT", "claude-sonnet-5")  # category override
    client = _classifier("quick_fact", "low", needs_live_data=True)
    decision = decide_route("what's the weather today", Mode.auto, client=client)
    assert decision.model == "claude-sonnet-5"
    assert decision.needs_live_data is False  # not OpenAI-served


def test_decide_route_heuristic_fallback_can_enable_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")

    def raise_classifier(**_kw):
        raise RuntimeError("classifier down")

    client = SimpleNamespace(responses=SimpleNamespace(create=raise_classifier))
    client.with_options = lambda **_kw: client  # type: ignore[attr-defined]

    decision = decide_route("what's the weather today", Mode.auto, client=client)
    assert decision.needs_live_data is True


def test_decide_route_explicit_modes_never_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No classifier consulted for fast/smart/budget/forced -> no signal at all,
    # regardless of WEB_SEARCH.
    monkeypatch.setenv("WEB_SEARCH", "true")
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model")
    assert decide_route("weather today", Mode.fast).needs_live_data is False
    assert decide_route("weather today", Mode.smart).needs_live_data is False
    assert decide_route("weather today", Mode.budget).needs_live_data is False
    assert (
        decide_route("weather today", Mode.auto, forced_model="gpt-5").needs_live_data
        is False
    )


def test_decide_route_prefilter_never_searches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    client = _classifier("coding", "high", needs_live_data=True)  # would never be asked
    # A pure greeting fires the prefilter and skips the classifier entirely.
    decision = decide_route("hi there", Mode.auto, client=client)
    assert decision.needs_live_data is False


# --- orchestrator: cache skip for freshness-sensitive answers -----------------


def test_run_orchestrator_skips_cache_when_needs_live_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    from app.routing import RouteDecision

    decision = RouteDecision(
        model="gpt-5",
        mode_used="auto->fast",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="low",
        needs_live_data=True,
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(
        orchestrator, "_call_model", lambda **_kw: "the weather is sunny"
    )

    result = run_orchestrator(AskRequest(question="weather today", mode=Mode.auto))

    assert result.answer == "the weather is sunny"
    # Nothing was cached: a fresh call to the SAME question must not hit a stale
    # cache entry (would require another live search, so caching would poison it).
    key = cache.make_key("weather today", "auto")
    assert cache.get(key) is None


def test_run_orchestrator_caches_normally_when_not_live_data(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    from app.routing import RouteDecision

    decision = RouteDecision(
        model="gpt-5",
        mode_used="auto->fast",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="low",
        needs_live_data=False,
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "42")

    run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.auto))

    key = cache.make_key("what is 2+2", "auto")
    assert cache.get(key) is not None  # normal answers still cache as before


# --- orchestrator: sources reach AskResponse / the SSE done frame ------------


def test_run_orchestrator_populates_sources(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routing import RouteDecision

    decision = RouteDecision(
        model="gpt-5",
        mode_used="auto->fast",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="low",
        needs_live_data=True,
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        citations = kwargs["citations"]
        assert kwargs["web_search"] is True
        citations.append({"title": "Weather Site", "url": "https://weather.example"})
        return "sunny"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="weather today", mode=Mode.auto))

    assert result.sources is not None
    assert result.sources[0].url == "https://weather.example"
    assert result.sources[0].title == "Weather Site"


def test_stream_orchestrator_done_frame_includes_sources(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routing import RouteDecision

    decision = RouteDecision(
        model="gpt-5",
        mode_used="auto->fast",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="low",
        needs_live_data=True,
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs) -> Iterator[str]:
        kwargs["citations"].append({"title": "T", "url": "https://s.example"})
        yield "sunny"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(AskRequest(question="weather today", mode=Mode.auto))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["sources"] == [{"title": "T", "url": "https://s.example"}]


def test_stream_orchestrator_omits_sources_key_when_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["hi"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))
    done = events[-1]
    assert "sources" not in done["data"]


# --- persistence: sources round-trip through the database --------------------


def test_add_message_and_list_messages_roundtrip_sources(db_path: Path) -> None:
    from app.database import create_conversation

    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="sunny",
        sources=json.dumps([{"title": "T", "url": "https://s.example"}]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["sources"]) == [
        {"title": "T", "url": "https://s.example"}
    ]


def test_add_message_without_sources_stores_null(db_path: Path) -> None:
    from app.database import create_conversation

    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="assistant", content="hi")
    messages = list_messages(conv["id"])
    assert messages[0]["sources"] is None


# --- HTTP integration: sources persist through ask / stream / regenerate -----


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse, Source

    def fake_run(req, routing_question=None, owner=None):
        return AskResponse(
            answer="sunny",
            mode_used="auto->fast",
            notes="n",
            sources=[Source(title="T", url="https://s.example")],
        )

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "weather"})

    assert r.status_code == 200
    assert r.json()["sources"] == [{"title": "T", "url": "https://s.example"}]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["sources"] == [{"title": "T", "url": "https://s.example"}]


def test_stream_ask_persists_sources_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None):
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "sunny",
                "mode_used": "auto->fast",
                "notes": "n",
                "sources": [{"title": "T", "url": "https://s.example"}],
            },
        }

    monkeypatch.setattr("app.main.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "weather"})
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["sources"] == [{"title": "T", "url": "https://s.example"}]
