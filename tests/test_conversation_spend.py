"""A conversation must report what it ACTUALLY cost, not only what its saved
messages cost.

The displayed total was summed from messages, so every call that spent money
without producing one — a truncated empty answer, a discarded regenerate, a
cancelled stream — was invisible in it. A real session showed $0.1014 in the
footer against $0.5742 billed: 82% of the conversation's cost, unaccounted
for, in the app whose headline feature is cost transparency.

spend_log now carries the conversation_id, and GET
/v1/conversations/{id}/spend reports the true total plus the part with no
message behind it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
import app.routers.messages
from app.database import add_message, conversation_spend, record_spend
from app.schemas import AskResponse
from app.spend_context import conversation_scope, current_conversation_id


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


# --- database.conversation_spend ---------------------------------------------


def test_conversation_spend_sums_only_this_conversation(client: TestClient) -> None:
    mine = _create(client)
    theirs = _create(client)

    record_spend("o", "m", 100, 50, 0.10, mine)
    record_spend("o", "m", 200, 60, 0.25, mine)
    record_spend("o", "m", 999, 999, 9.99, theirs)
    record_spend("o", "m", 10, 10, 0.01, None)  # stateless /v1/ask call

    totals = conversation_spend(mine)
    assert totals["cost_usd"] == pytest.approx(0.35)
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 110


def test_conversation_spend_is_zero_for_a_conversation_with_no_calls(
    client: TestClient,
) -> None:
    assert conversation_spend(_create(client))["cost_usd"] == pytest.approx(0.0)


# --- the endpoint -------------------------------------------------------------


def test_spend_endpoint_reports_cost_with_no_message_behind_it(
    client: TestClient,
) -> None:
    """The whole point: a call that was billed but produced no saved message."""
    cid = _create(client)
    add_message(
        conversation_id=cid, role="assistant", content="an answer", cost_usd=0.10
    )
    record_spend("o", "m", 10, 10, 0.10, cid)  # that answer
    record_spend("o", "m", 10, 10, 0.47, cid)  # five dropped empty answers

    body = client.get(f"/v1/conversations/{cid}/spend").json()
    assert body["cost_usd"] == pytest.approx(0.57)
    assert body["unattributed_cost_usd"] == pytest.approx(0.47)


def test_spend_endpoint_reports_nothing_unattributed_when_all_calls_answered(
    client: TestClient,
) -> None:
    cid = _create(client)
    add_message(conversation_id=cid, role="assistant", content="a", cost_usd=0.10)
    record_spend("o", "m", 10, 10, 0.10, cid)

    body = client.get(f"/v1/conversations/{cid}/spend").json()
    assert body["unattributed_cost_usd"] == pytest.approx(0.0)


def test_spend_endpoint_never_reports_negative_unattributed(
    client: TestClient,
) -> None:
    """A cached hit costs the conversation nothing but persists with the
    original call's cost_usd for display, so the message-derived sum can
    legitimately exceed logged spend. That is not negative waste."""
    cid = _create(client)
    add_message(conversation_id=cid, role="assistant", content="cached", cost_usd=0.50)

    body = client.get(f"/v1/conversations/{cid}/spend").json()
    assert body["unattributed_cost_usd"] == pytest.approx(0.0)


def test_spend_endpoint_404s_for_someone_elses_conversation(
    client: TestClient,
) -> None:
    assert client.get("/v1/conversations/999999/spend").status_code == 404


# --- attribution actually happens on the ask path -----------------------------


def test_dropped_empty_answer_is_still_attributed_to_the_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, end to end: the answer is empty so no message is saved —
    but the money must still show up against the conversation."""

    def fake_run(
        req: Any,
        routing_question: Any = None,
        owner: Any = None,
        history: str = "",
        **_kw: Any,
    ) -> AskResponse:
        # Reads the ambient scope the router established, exactly as the real
        # _record_spend does.
        record_spend(
            owner, "claude-sonnet-5", 11620, 4000, 0.0985, current_conversation_id()
        )
        return AskResponse(answer="", mode_used="auto->smart", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run)

    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "spreadsheet"})

    # No assistant message (the guard did its job)...
    roles = [m["role"] for m in client.get(f"/v1/conversations/{cid}/messages").json()]
    assert roles == ["user"]
    # ...but the spend is visible against this conversation, not lost.
    body = client.get(f"/v1/conversations/{cid}/spend").json()
    assert body["cost_usd"] == pytest.approx(0.0985)
    assert body["unattributed_cost_usd"] == pytest.approx(0.0985)


def test_orchestrator_threads_conversation_id_into_the_spend_log(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a stub of the plumbing — the real run_orchestrator writing a real
    spend_log row against the conversation the router handed it."""
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: Any) -> str:
        usage = kwargs["usage"]
        usage.input_tokens = 1000
        usage.output_tokens = 500
        return "answered"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi", "mode": "fast"})

    totals = conversation_spend(cid)
    assert totals["input_tokens"] == 1000
    assert totals["output_tokens"] == 500


# --- the scope itself ---------------------------------------------------------


def test_scope_is_none_outside_a_conversation_request() -> None:
    """The stateless /v1/ask endpoints have no conversation; spend logs
    unattributed rather than landing on whichever conversation ran last."""
    assert current_conversation_id() is None


def test_scope_resets_even_when_the_body_raises() -> None:
    """A leaked scope would misattribute the NEXT request's spend on this
    thread — the failure mode worth guarding, since it is silent."""
    with pytest.raises(RuntimeError):
        with conversation_scope(7):
            assert current_conversation_id() == 7
            raise RuntimeError("boom")
    assert current_conversation_id() is None


def test_scopes_nest() -> None:
    with conversation_scope(1):
        with conversation_scope(2):
            assert current_conversation_id() == 2
        assert current_conversation_id() == 1
