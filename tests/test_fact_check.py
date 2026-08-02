"""Fact-check lookup (app/fact_check.py): the phrase heuristic, Google Fact
Check Tools API call/parsing, orchestrator gating, and end-to-end persistence.

Independent of which model answers — same "standalone call gated by a
phrase heuristic" design as the Gemini/Imagen image-generation path, since
neither OpenAI nor Anthropic offers a hosted tool for this.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import fact_check
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode


# --- config / flags -----------------------------------------------------------


def test_fact_check_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACT_CHECK", raising=False)
    assert fact_check.fact_check_enabled() is False
    monkeypatch.setenv("FACT_CHECK", "true")
    assert fact_check.fact_check_enabled() is True
    monkeypatch.setenv("FACT_CHECK", "false")
    assert fact_check.fact_check_enabled() is False


# --- looks_like_fact_check_request ---------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "fact check: the moon landing was faked",
        "Is it true that vaccines cause autism?",
        "TRUE OR FALSE: coffee stunts your growth",
        "can you debunk this claim about 5G",
        "please verify this claim for me",
        "did this really happen — a man bit a shark",
    ],
)
def test_looks_like_fact_check_request_matches(question: str) -> None:
    assert fact_check.looks_like_fact_check_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "what's the capital of France?",
        "write me a poem about autumn",
        "help me debug this Python function",
    ],
)
def test_looks_like_fact_check_request_no_match(question: str) -> None:
    assert fact_check.looks_like_fact_check_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "factcheck this headline about the election",
        "is it true this recipe actually works",
        "is this true or just a rumor?",
        "is that true about the new tax law?",
        "can you verify the claim in this article",
        "did that really happen in 1969?",
        "is this a hoax going around on social media",
        "is that a hoax, the story about the shark attack",
    ],
)
def test_looks_like_fact_check_request_matches_every_remaining_phrase(
    question: str,
) -> None:
    """Every phrase in _FACT_CHECK_PHRASES gets at least one should-fire
    fixture — the parametrized test above only exercised 6 of 16."""
    assert fact_check.looks_like_fact_check_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Incidental uses of the same words/roots that must NOT fire --
        # same style as math_solve's "integrate this feedback" trap. Bare
        # "debunk" is a deliberate single-word trigger (see the phrase
        # list's own comment on erring toward precision), so it's not
        # included here -- these avoid it entirely rather than testing
        # around it.
        "true, false, or maybe: what's your favorite color?",
        "can you check my facts and figures in this budget spreadsheet",
        "is this a good hoax costume idea for Halloween?",
        "verify my email address is spelled correctly: john@example.com",
        "is this claim form filled out correctly for my insurance?",
        "what's your claim to fame?",
    ],
)
def test_looks_like_fact_check_request_incidental_word_reuse_must_not_match(
    question: str,
) -> None:
    """A trap set: text that superficially resembles a trigger phrase (or
    reuses one of its words/roots in an unrelated sense) but isn't actually
    asking to verify a real-world claim -- must not fire. 'is this claim
    form...'/'claim to fame' pin the fix for a real bug this exact trap
    style found: a bare 'is this claim' phrase used to be in
    _FACT_CHECK_PHRASES and false-positived on any sentence containing that
    literal substring (see the BUG HISTORY note there)."""
    assert fact_check.looks_like_fact_check_request(question) is False


# --- check_claim: gating --------------------------------------------------------


def test_check_claim_returns_empty_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_FACT_CHECK_API_KEY", raising=False)
    assert fact_check.check_claim("the moon landing was faked") == []


def test_check_claim_returns_empty_for_blank_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    assert fact_check.check_claim("   ") == []


# --- check_claim: HTTP call + parsing -------------------------------------------


def _fake_response(payload: dict, status: int = 200) -> types.SimpleNamespace:
    def raise_for_status() -> None:
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    return types.SimpleNamespace(
        raise_for_status=raise_for_status, json=lambda: payload
    )


def test_check_claim_parses_claims_and_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    payload = {
        "claims": [
            {
                "text": "The moon landing was faked",
                "claimReview": [
                    {
                        "publisher": {"name": "Snopes"},
                        "url": "https://snopes.com/fact-check/moon-landing",
                        "textualRating": "False",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        fact_check.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = fact_check.check_claim("was the moon landing faked")
    assert results == [
        {
            "claim": "The moon landing was faked",
            "rating": "False",
            "publisher": "Snopes",
            "url": "https://snopes.com/fact-check/moon-landing",
        }
    ]


def test_check_claim_passes_query_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    captured: dict = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _fake_response({"claims": []})

    monkeypatch.setattr(fact_check.httpx, "get", fake_get)
    fact_check.check_claim("some claim")
    assert captured["params"]["query"] == "some claim"
    assert captured["params"]["key"] == "key123"
    assert captured["url"] == fact_check._FACT_CHECK_URL


def test_check_claim_skips_non_http_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    payload = {
        "claims": [
            {
                "text": "evil claim",
                "claimReview": [
                    {
                        "publisher": {"name": "Evil"},
                        "url": "javascript:alert(1)",
                        "textualRating": "False",
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        fact_check.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    assert fact_check.check_claim("evil claim") == []


def test_check_claim_falls_back_to_review_title_when_claim_text_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    payload = {
        "claims": [
            {
                "claimReview": [
                    {
                        "title": "Review title as fallback",
                        "publisher": {},
                        "url": "https://example.com/review",
                    }
                ]
            }
        ]
    }
    monkeypatch.setattr(
        fact_check.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = fact_check.check_claim("q")
    assert results[0]["claim"] == "Review title as fallback"
    assert results[0]["rating"] is None
    assert results[0]["publisher"] is None


def test_check_claim_caps_at_max_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    payload = {
        "claims": [
            {
                "text": f"claim {i}",
                "claimReview": [
                    {
                        "publisher": {"name": "P"},
                        "url": f"https://example.com/{i}",
                        "textualRating": "False",
                    }
                ],
            }
            for i in range(10)
        ]
    }
    monkeypatch.setattr(
        fact_check.httpx, "get", lambda *a, **k: _fake_response(payload)
    )
    results = fact_check.check_claim("q")
    assert len(results) == fact_check._MAX_RESULTS


def test_check_claim_tolerates_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")

    def boom(*a, **k):
        raise RuntimeError("network error")

    monkeypatch.setattr(fact_check.httpx, "get", boom)
    assert fact_check.check_claim("q") == []


def test_check_claim_tolerates_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    monkeypatch.setattr(
        fact_check.httpx,
        "get",
        lambda *a, **k: _fake_response({"claims": [{"claimReview": "not-a-list"}]}),
    )
    assert fact_check.check_claim("q") == []


def test_check_claim_no_claims_key_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_FACT_CHECK_API_KEY", "key123")
    monkeypatch.setattr(fact_check.httpx, "get", lambda *a, **k: _fake_response({}))
    assert fact_check.check_claim("q") == []


# --- format_note -----------------------------------------------------------------


def test_format_note_singular_and_plural() -> None:
    assert "a related fact-check" in fact_check.format_note(1)
    assert "1 related fact-check" not in fact_check.format_note(1)
    assert "2 related fact-checks" in fact_check.format_note(2)


# --- orchestrator: gating + response wiring -------------------------------------


def test_run_orchestrator_fact_check_wanted_requires_enabled_and_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_check_claim(query: str) -> list[dict]:
        seen["called"] = True
        return []

    monkeypatch.setattr(orchestrator, "check_claim", fake_check_claim)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    # Disabled entirely: heuristic matches, but FACT_CHECK is off.
    monkeypatch.delenv("FACT_CHECK", raising=False)
    run_orchestrator(
        AskRequest(question="fact check: the moon landing", mode=Mode.smart)
    )
    assert "called" not in seen

    # Enabled, but the question doesn't look like a fact-check request.
    monkeypatch.setenv("FACT_CHECK", "true")
    run_orchestrator(AskRequest(question="what's the weather", mode=Mode.smart))
    assert "called" not in seen

    # Enabled AND matches the heuristic.
    run_orchestrator(
        AskRequest(question="fact check: the moon landing", mode=Mode.smart)
    )
    assert seen.get("called") is True


def test_run_orchestrator_returns_and_composes_fact_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "the model's answer")
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda query: [
            {
                "claim": "the moon landing was faked",
                "rating": "False",
                "publisher": "Snopes",
                "url": "https://snopes.com/x",
            }
        ],
    )

    result = run_orchestrator(
        AskRequest(question="fact check: the moon landing", mode=Mode.smart)
    )

    assert result.fact_checks is not None
    assert result.fact_checks[0].claim == "the moon landing was faked"
    assert result.fact_checks[0].rating == "False"
    assert "found" in result.answer.lower()
    assert "the model's answer" in result.answer


def test_run_orchestrator_no_fact_checks_key_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACT_CHECK", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.fact_checks is None


def test_run_orchestrator_skips_cache_when_fact_checks_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("FACT_CHECK", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda query: [
            {"claim": "c", "rating": "r", "publisher": "p", "url": "https://x"}
        ],
    )

    run_orchestrator(AskRequest(question="fact check: something", mode=Mode.smart))

    key = cache.make_key("fact check: something", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_fact_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda query: [
            {"claim": "c", "rating": "r", "publisher": "p", "url": "https://x"}
        ],
    )

    events = list(
        stream_orchestrator(
            AskRequest(question="fact check: something", mode=Mode.smart)
        )
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["fact_checks"] == [
        {"claim": "c", "rating": "r", "publisher": "p", "url": "https://x"}
    ]


def test_stream_orchestrator_omits_fact_checks_key_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FACT_CHECK", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert "fact_checks" not in done["data"]


# --- HTTP: end-to-end persistence ----------------------------------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_fact_checks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse, FactCheck

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="False. Found 1 related fact-check.",
            mode_used="smart",
            notes="n",
            fact_checks=[
                FactCheck(
                    claim="the moon landing was faked",
                    rating="False",
                    publisher="Snopes",
                    url="https://snopes.com/x",
                )
            ],
        )

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "fact check: the moon landing"},
    )

    assert r.status_code == 200
    assert r.json()["fact_checks"] == [
        {
            "claim": "the moon landing was faked",
            "rating": "False",
            "publisher": "Snopes",
            "url": "https://snopes.com/x",
        }
    ]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["fact_checks"] == [
        {
            "claim": "the moon landing was faked",
            "rating": "False",
            "publisher": "Snopes",
            "url": "https://snopes.com/x",
        }
    ]


def test_stream_ask_persists_fact_checks_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "False.",
                "mode_used": "smart",
                "notes": "n",
                "fact_checks": [
                    {
                        "claim": "c",
                        "rating": "False",
                        "publisher": "Snopes",
                        "url": "https://snopes.com/x",
                    }
                ],
            },
        }

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "fact check: something"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["fact_checks"] == [
        {
            "claim": "c",
            "rating": "False",
            "publisher": "Snopes",
            "url": "https://snopes.com/x",
        }
    ]


# --- Settings registry -----------------------------------------------------------


def test_fact_check_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "FACT_CHECK")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
