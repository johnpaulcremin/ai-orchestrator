"""Academic-search lookup (app/academic_search.py): the phrase heuristic,
OpenAlex API call/parsing, orchestrator gating, and end-to-end persistence.

Same "standalone call gated by a phrase heuristic" design as
tests/test_fact_check.py — see that module's docstring for the full
rationale.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import academic_search
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode


# --- config / flags -----------------------------------------------------------


def test_academic_search_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACADEMIC_SEARCH", raising=False)
    assert academic_search.academic_search_enabled() is False
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    assert academic_search.academic_search_enabled() is True
    monkeypatch.setenv("ACADEMIC_SEARCH", "false")
    assert academic_search.academic_search_enabled() is False


# --- looks_like_academic_search_request -----------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what papers on climate change adaptation exist",
        "any studies about intermittent fasting?",
        "is there academic research on remote work productivity",
        "looking for peer-reviewed articles on gut microbiome",
        "can you point me to research papers on transformer architectures",
        "give me a literature review on reinforcement learning",
    ],
)
def test_looks_like_academic_search_request_matches(question: str) -> None:
    assert academic_search.looks_like_academic_search_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "research my competitors and summarize their pricing",
        "what's the capital of France?",
        "help me research a new laptop to buy",
        "write me a poem about autumn",
    ],
)
def test_looks_like_academic_search_request_no_match(question: str) -> None:
    assert academic_search.looks_like_academic_search_request(question) is False


# --- search_papers: gating -------------------------------------------------------


def test_search_papers_returns_empty_for_blank_query() -> None:
    assert academic_search.search_papers("   ") == []


# --- search_papers: HTTP call + parsing ------------------------------------------


def _fake_response(payload: dict, status: int = 200) -> types.SimpleNamespace:
    def raise_for_status() -> None:
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    return types.SimpleNamespace(
        raise_for_status=raise_for_status, json=lambda: payload
    )


def test_search_papers_parses_works(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "results": [
            {
                "display_name": "Attention Is All You Need",
                "authorships": [
                    {"author": {"display_name": "Ashish Vaswani"}},
                    {"author": {"display_name": "Noam Shazeer"}},
                ],
                "publication_year": 2017,
                "primary_location": {"source": {"display_name": "NeurIPS"}},
                "cited_by_count": 90000,
                "open_access": {"oa_url": "https://arxiv.org/abs/1706.03762"},
                "abstract_inverted_index": {
                    "The": [0],
                    "transformer": [1],
                    "model.": [2],
                },
            }
        ]
    }
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("attention is all you need")
    assert results == [
        {
            "title": "Attention Is All You Need",
            "authors": "Ashish Vaswani, Noam Shazeer",
            "year": 2017,
            "venue": "NeurIPS",
            "citation_count": 90000,
            "url": "https://arxiv.org/abs/1706.03762",
            "abstract_snippet": "The transformer model.",
        }
    ]


def test_search_papers_passes_query(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _fake_response({"results": []})

    monkeypatch.setattr(academic_search.httpx, "get", fake_get)
    academic_search.search_papers("some query")
    assert captured["params"]["search"] == "some query"
    assert captured["url"] == academic_search._OPENALEX_URL


def test_search_papers_truncates_authors_beyond_max_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "results": [
            {
                "display_name": "A Big Collaboration",
                "authorships": [
                    {"author": {"display_name": f"Author {i}"}} for i in range(6)
                ],
            }
        ]
    }
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("q")
    assert results[0]["authors"] == "Author 0, Author 1, Author 2 et al."


def test_search_papers_falls_back_to_doi_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "results": [
            {
                "display_name": "Some Paper",
                "doi": "10.1000/xyz123",
            }
        ]
    }
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("q")
    assert results[0]["url"] == "https://doi.org/10.1000/xyz123"


def test_search_papers_skips_works_with_no_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"results": [{"display_name": ""}, {"display_name": "Real Title"}]}
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("q")
    assert len(results) == 1
    assert results[0]["title"] == "Real Title"


def test_search_papers_caps_at_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"results": [{"display_name": f"Paper {i}"} for i in range(10)]}
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("q")
    assert len(results) == academic_search._MAX_RESULTS


def test_search_papers_tolerates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(academic_search.httpx, "get", boom)
    assert academic_search.search_papers("q") == []


def test_search_papers_tolerates_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        academic_search.httpx,
        "get",
        lambda *a, **k: _fake_response({"results": [None]}),
    )
    assert academic_search.search_papers("q") == []


def test_search_papers_no_results_key_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response({})
    )
    assert academic_search.search_papers("q") == []


def test_search_papers_no_authorships_gives_none_authors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"results": [{"display_name": "Solo work"}]}
    monkeypatch.setattr(
        academic_search.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = academic_search.search_papers("q")
    assert results[0]["authors"] is None
    assert results[0]["abstract_snippet"] is None
    assert results[0]["url"] is None


# --- format_note -----------------------------------------------------------------


def test_format_note_singular_and_plural() -> None:
    assert "a related academic paper" in academic_search.format_note(1)
    assert "1 related academic paper" not in academic_search.format_note(1)
    assert "2 related academic papers" in academic_search.format_note(2)


# --- orchestrator: gating + response wiring -------------------------------------


def test_run_orchestrator_academic_search_wanted_requires_enabled_and_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_search_papers(query: str) -> list[dict]:
        seen["called"] = True
        return []

    monkeypatch.setattr(orchestrator, "search_papers", fake_search_papers)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    # Disabled entirely: heuristic matches, but ACADEMIC_SEARCH is off.
    monkeypatch.delenv("ACADEMIC_SEARCH", raising=False)
    run_orchestrator(AskRequest(question="papers on climate change", mode=Mode.smart))
    assert "called" not in seen

    # Enabled, but the question doesn't look like an academic-search request.
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    run_orchestrator(AskRequest(question="what's the weather", mode=Mode.smart))
    assert "called" not in seen

    # Enabled AND matches the heuristic.
    run_orchestrator(AskRequest(question="papers on climate change", mode=Mode.smart))
    assert seen.get("called") is True


def test_run_orchestrator_returns_and_composes_academic_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "the model's answer")
    monkeypatch.setattr(
        orchestrator,
        "search_papers",
        lambda query: [
            {
                "title": "Climate Adaptation Strategies",
                "authors": "A. Researcher",
                "year": 2022,
                "venue": "Nature",
                "citation_count": 42,
                "url": "https://example.com/paper",
                "abstract_snippet": "This paper examines...",
            }
        ],
    )

    result = run_orchestrator(
        AskRequest(question="papers on climate adaptation", mode=Mode.smart)
    )

    assert result.academic_results is not None
    assert result.academic_results[0].title == "Climate Adaptation Strategies"
    assert result.academic_results[0].year == 2022
    assert "found" in result.answer.lower()
    assert "the model's answer" in result.answer


def test_run_orchestrator_no_academic_results_key_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACADEMIC_SEARCH", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.academic_results is None


def test_run_orchestrator_skips_cache_when_academic_results_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")
    monkeypatch.setattr(
        orchestrator,
        "search_papers",
        lambda query: [{"title": "t"}],
    )

    run_orchestrator(AskRequest(question="papers on something", mode=Mode.smart))

    key = cache.make_key("papers on something", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_academic_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))
    monkeypatch.setattr(
        orchestrator,
        "search_papers",
        lambda query: [{"title": "t"}],
    )

    events = list(
        stream_orchestrator(AskRequest(question="papers on something", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["academic_results"] == [{"title": "t"}]


def test_stream_orchestrator_omits_academic_results_key_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACADEMIC_SEARCH", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert "academic_results" not in done["data"]


# --- HTTP: end-to-end persistence ----------------------------------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_academic_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AcademicResult, AskResponse

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="Found 1 related academic paper.",
            mode_used="smart",
            notes="n",
            academic_results=[
                AcademicResult(
                    title="Climate Adaptation Strategies",
                    authors="A. Researcher",
                    year=2022,
                    venue="Nature",
                    citation_count=42,
                    url="https://example.com/paper",
                    abstract_snippet="This paper examines...",
                )
            ],
        )

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "papers on climate adaptation"},
    )

    assert r.status_code == 200
    assert r.json()["academic_results"] == [
        {
            "title": "Climate Adaptation Strategies",
            "authors": "A. Researcher",
            "year": 2022,
            "venue": "Nature",
            "citation_count": 42,
            "url": "https://example.com/paper",
            "abstract_snippet": "This paper examines...",
        }
    ]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["academic_results"] == [
        {
            "title": "Climate Adaptation Strategies",
            "authors": "A. Researcher",
            "year": 2022,
            "venue": "Nature",
            "citation_count": 42,
            "url": "https://example.com/paper",
            "abstract_snippet": "This paper examines...",
        }
    ]


def test_stream_ask_persists_academic_results_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Found 1.",
                "mode_used": "smart",
                "notes": "n",
                "academic_results": [
                    {
                        "title": "t",
                        "authors": None,
                        "year": None,
                        "venue": None,
                        "citation_count": None,
                        "url": None,
                        "abstract_snippet": None,
                    }
                ],
            },
        }

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "papers on something"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["academic_results"] == [
        {
            "title": "t",
            "authors": None,
            "year": None,
            "venue": None,
            "citation_count": None,
            "url": None,
            "abstract_snippet": None,
        }
    ]


# --- Settings registry -----------------------------------------------------------


def test_academic_search_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "ACADEMIC_SEARCH")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
