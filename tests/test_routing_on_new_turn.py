"""Auto mode must route on the raw new user turn, not the assembled context
prompt. A code fence (or any smart-marker) in earlier history must not force
every later turn to the smart tier, and the classifier must categorize the
question rather than the truncated conversation history.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main
import app.orchestrator
from app.database import add_message
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.routing import RouteDecision
from app.schemas import AskRequest, AskResponse, Mode

_DECISION = RouteDecision(
    model="fast-x",
    mode_used="auto->fast",
    notes="scripted",
    max_output_tokens=100,
    reasoning_effort="low",
)


# --- orchestrator-level contract --------------------------------------------


def test_run_orchestrator_routes_on_routing_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_decide_route(
        question: str,
        mode: Mode,
        client: object = None,
        forced_model: str | None = None,
    ) -> RouteDecision:
        seen["route_q"] = question
        return _DECISION

    def fake_call_model(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage: object = None,
    ) -> str:
        seen["answer_q"] = question
        return "ok"

    monkeypatch.setattr(app.orchestrator, "decide_route", fake_decide_route)
    monkeypatch.setattr(app.orchestrator, "_call_model", fake_call_model)

    resp = run_orchestrator(
        AskRequest(question="FULL CONTEXT PROMPT with ```fence```", mode=Mode.auto),
        routing_question="thanks",
    )

    assert seen["route_q"] == "thanks"  # routed on the new turn
    assert seen["answer_q"] == "FULL CONTEXT PROMPT with ```fence```"  # answered on it
    assert resp.answer == "ok"


def test_run_orchestrator_defaults_routing_to_req_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stateless path passes no routing_question -> route on req.question."""
    seen: dict[str, str] = {}

    def fake_decide_route(
        question: str,
        mode: Mode,
        client: object = None,
        forced_model: str | None = None,
    ) -> RouteDecision:
        seen["route_q"] = question
        return _DECISION

    monkeypatch.setattr(app.orchestrator, "decide_route", fake_decide_route)
    monkeypatch.setattr(app.orchestrator, "_call_model", lambda *a, **k: "ok")

    run_orchestrator(AskRequest(question="raw question", mode=Mode.auto))
    assert seen["route_q"] == "raw question"


def test_stream_orchestrator_routes_on_routing_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_decide_route(
        question: str,
        mode: Mode,
        client: object = None,
        forced_model: str | None = None,
    ) -> RouteDecision:
        seen["route_q"] = question
        return _DECISION

    def fake_stream_model(
        model: str,
        question: str,
        max_output_tokens: int,
        reasoning_effort: str = "",
        usage: object = None,
    ) -> Iterator[str]:
        seen["answer_q"] = question
        yield "ok"

    monkeypatch.setattr(app.orchestrator, "decide_route", fake_decide_route)
    monkeypatch.setattr(app.orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(
            AskRequest(question="CTX ```fence```", mode=Mode.auto),
            routing_question="hello",
        )
    )

    assert seen["route_q"] == "hello"
    assert seen["answer_q"] == "CTX ```fence```"
    assert events[-1]["event"] == "done"


# --- HTTP integration: main.py threads the raw turn through ------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def _seed_fence_history(cid: int) -> None:
    """Prior history whose USER turn contains a code fence (a smart-marker)."""
    add_message(conversation_id=cid, role="user", content="```python\nx = 1\n```")
    add_message(conversation_id=cid, role="assistant", content="ok")


def test_conversation_ask_threads_new_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str | None] = {}

    def fake_run(
        req: AskRequest, routing_question: str | None = None, owner: str | None = None
    ) -> AskResponse:
        captured["req_q"] = req.question
        captured["routing_q"] = routing_question
        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run)

    cid = _create(client)
    _seed_fence_history(cid)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "thanks"})

    assert r.status_code == 200
    assert captured["routing_q"] == "thanks"  # route on the new turn only
    assert "x = 1" in str(captured["req_q"])  # but answer on the full context
    assert captured["req_q"] != "thanks"


def test_conversation_stream_threads_new_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str | None] = {}

    def fake_stream(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
    ) -> Iterator[dict[str, object]]:
        captured["req_q"] = req.question
        captured["routing_q"] = routing_question
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}}
        yield {
            "event": "done",
            "data": {"answer": "a", "mode_used": "auto->fast", "notes": "n"},
        }

    monkeypatch.setattr(app.main, "stream_orchestrator", fake_stream)

    cid = _create(client)
    _seed_fence_history(cid)
    r = client.post(f"/v1/conversations/{cid}/ask/stream", json={"question": "thanks"})

    assert r.status_code == 200
    assert captured["routing_q"] == "thanks"
    assert "x = 1" in str(captured["req_q"])


def test_regenerate_threads_last_user_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str | None] = {}

    def fake_run(
        req: AskRequest, routing_question: str | None = None, owner: str | None = None
    ) -> AskResponse:
        captured["req_q"] = req.question
        captured["routing_q"] = routing_question
        return AskResponse(answer="regen", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run)

    cid = _create(client)
    _seed_fence_history(cid)  # a fenced earlier turn...
    add_message(conversation_id=cid, role="user", content="what is 2+2")
    add_message(conversation_id=cid, role="assistant", content="old")

    r = client.post(f"/v1/conversations/{cid}/regenerate", json={})

    assert r.status_code == 200
    assert captured["routing_q"] == "what is 2+2"  # the last user turn, not history
    assert "x = 1" in str(captured["req_q"])  # context still carries the fence
