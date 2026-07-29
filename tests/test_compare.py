"""Model comparison (POST /v1/compare): the same question dispatched to
several specific models concurrently, with each answer reported alongside
its cost/tokens/latency — a direct way to see what multi-provider routing
trades off. Nothing here is persisted as a conversation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routers.ask
from app.schemas import AskRequest, AskResponse


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=f"answer from {req.model}",
            mode_used=f"forced:{req.model}",
            notes="n",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
        )

    monkeypatch.setattr(app.routers.ask, "run_orchestrator", fake_run_orchestrator)
    return calls


def _compare(client: TestClient, question: str, models: list[str]):
    return client.post("/v1/compare", json={"question": question, "models": models})


def test_compare_dispatches_to_each_model_in_order(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    res = _compare(client, "what is 2+2?", ["gpt-5", "claude-sonnet-5"])
    assert res.status_code == 200
    body = res.json()
    assert body["question"] == "what is 2+2?"
    assert [r["model"] for r in body["results"]] == ["gpt-5", "claude-sonnet-5"]
    assert body["results"][0]["answer"] == "answer from gpt-5"
    assert body["results"][1]["answer"] == "answer from claude-sonnet-5"


def test_compare_forwards_the_forced_model_and_question(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    _compare(
        client, "translate hello to french", ["gpt-5", "claude-sonnet-5", "gpt-5-mini"]
    )
    # Dispatched concurrently now (see the compare() docstring), so the
    # ORDER these land in `calls` isn't guaranteed to match request order —
    # only that all three were called. Result order IS still guaranteed
    # (see test_compare_dispatches_to_each_model_in_order), since that's
    # reconstructed from executor.map's return value, not this side channel.
    assert {c.model for c in orchestrator_calls} == {
        "gpt-5",
        "claude-sonnet-5",
        "gpt-5-mini",
    }
    assert all(c.question == "translate hello to french" for c in orchestrator_calls)


def test_compare_reports_cost_tokens_and_elapsed_ms(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    res = _compare(client, "hi", ["gpt-5", "claude-sonnet-5"])
    result = res.json()["results"][0]
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 20
    assert result["cost_usd"] == 0.01
    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0


def test_compare_isolates_a_single_model_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flaky_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        **_kw: object,
    ) -> AskResponse:
        if req.model == "broken-model":
            return AskResponse(
                answer="", mode_used="forced:broken-model", notes="no API key"
            )
        return AskResponse(answer="ok", mode_used=f"forced:{req.model}", notes="n")

    monkeypatch.setattr(app.routers.ask, "run_orchestrator", flaky_run_orchestrator)

    res = _compare(client, "hi", ["broken-model", "gpt-5"])
    assert res.status_code == 200
    results = res.json()["results"]
    assert results[0]["answer"] == ""
    assert results[0]["notes"] == "no API key"
    assert results[1]["answer"] == "ok"


def test_compare_requires_at_least_two_models(client: TestClient) -> None:
    res = _compare(client, "hi", ["gpt-5"])
    assert res.status_code == 422


def test_compare_rejects_more_than_four_models(client: TestClient) -> None:
    res = _compare(
        client, "hi", ["gpt-5", "gpt-5-mini", "claude-sonnet-5", "gpt-5-nano", "m5"]
    )
    assert res.status_code == 422


def test_compare_rejects_duplicate_models(client: TestClient) -> None:
    res = _compare(client, "hi", ["gpt-5", "gpt-5"])
    assert res.status_code == 422


def test_compare_rejects_empty_question(client: TestClient) -> None:
    res = _compare(client, "", ["gpt-5", "claude-sonnet-5"])
    assert res.status_code == 422


def test_compare_rejects_a_malformed_model_name(client: TestClient) -> None:
    res = _compare(client, "hi", ["gpt-5", "bad model!!"])
    assert res.status_code == 422


def test_compare_rejects_an_oversized_question(client: TestClient) -> None:
    res = _compare(client, "x" * 100_001, ["gpt-5", "claude-sonnet-5"])
    assert res.status_code == 422


def test_compare_dispatches_models_concurrently_not_sequentially(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the wall-clock characteristic the docstring claims — not just
    that results still come back correct, which would pass equally whether
    dispatch were sequential or concurrent."""
    import time

    delay_seconds = 0.3

    def slow_run_orchestrator(req: AskRequest, **_kw: object) -> AskResponse:
        time.sleep(delay_seconds)
        return AskResponse(answer=f"answer from {req.model}", mode_used="m", notes="n")

    monkeypatch.setattr(app.routers.ask, "run_orchestrator", slow_run_orchestrator)

    started = time.monotonic()
    res = _compare(client, "hi", ["gpt-5", "claude-sonnet-5", "gpt-5-mini"])
    wall_clock = time.monotonic() - started

    assert res.status_code == 200
    # Sequential would take ~3 * delay_seconds; concurrent takes ~1 *
    # delay_seconds plus overhead. The threshold sits well below the
    # sequential total but with generous headroom above one delay, so this
    # stays robust on a slow/loaded CI runner.
    assert wall_clock < delay_seconds * 2
