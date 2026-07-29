"""Live token/cost preview (POST /v1/estimate): what a question would cost if
sent, computed from the SAME worst-case estimate the DAILY_BUDGET_USD gate
uses on dispatch (see budget.estimate_worst_case) — without ever spending a
model or classifier call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.budget import estimate_worst_case


def _estimate(client: TestClient, question: str, mode: str = "auto"):
    return client.post("/v1/estimate", json={"question": question, "mode": mode})


# --- budget.estimate_worst_case: unit behavior --------------------------------


def test_estimate_worst_case_prices_input_and_max_output() -> None:
    tokens, cost = estimate_worst_case("gpt-5", 800, "a" * 400)
    assert tokens == 100  # 400 chars // 4
    assert cost is not None
    assert cost > 0


def test_estimate_worst_case_none_for_unpriced_model() -> None:
    tokens, cost = estimate_worst_case("some-totally-unknown-model", 800, "hi")
    assert tokens == 0
    assert cost is None


# --- HTTP: POST /v1/estimate ----------------------------------------------------


def test_estimate_fast_mode_resolves_fast_model(client: TestClient) -> None:
    res = _estimate(client, "hi", mode="fast")
    assert res.status_code == 200
    body = res.json()
    assert body["model"]
    assert body["mode_used"] == "fast"
    assert body["input_tokens_estimate"] >= 0
    assert body["output_tokens_estimate"] > 0


def test_estimate_smart_mode_resolves_smart_model(client: TestClient) -> None:
    res = _estimate(client, "hi", mode="smart")
    assert res.status_code == 200
    assert res.json()["mode_used"] == "smart"


def test_estimate_never_calls_the_classifier(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto mode must resolve via the free heuristic fallback, never the paid
    classifier — decide_route is called with client=None specifically so it
    can never reach that branch, but assert it directly too."""

    def boom(*args, **kwargs):
        raise AssertionError("estimate must never call the AI classifier")

    # _classify_with_ai is only ever reached when decide_route's `client` arg
    # is not None; patch it to blow up if that ever happens.
    import app.routing as routing_module

    monkeypatch.setattr(routing_module, "_classify_with_ai", boom)

    res = _estimate(
        client, "please write a detailed essay about something", mode="auto"
    )
    assert res.status_code == 200


def test_estimate_larger_question_yields_larger_or_equal_estimate(
    client: TestClient,
) -> None:
    short = _estimate(client, "hi", mode="fast").json()
    long = _estimate(client, "explain " * 200, mode="fast").json()
    assert long["input_tokens_estimate"] >= short["input_tokens_estimate"]


def test_estimate_rejects_empty_question(client: TestClient) -> None:
    res = _estimate(client, "")
    assert res.status_code == 422


def test_estimate_does_not_persist_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview call must never touch add_message/conversations — it's a
    stateless, read-only computation."""
    import app.routers.messages as main_module

    def boom(*args, **kwargs):
        raise AssertionError("estimate must never persist a message")

    monkeypatch.setattr(main_module, "add_message", boom)
    res = _estimate(client, "hi")
    assert res.status_code == 200
