"""The clarify loop: a reply to a clarifying question is not a new request.

Observed live, three clarifies in a row before the fourth turn answered:

    assistant [auto->clarify]: "Do you mean this assistant's strengths or the
                                chat app's strengths?"
    user:                      "this assistant's strengths"
    assistant [auto->clarify]: "Do you want strengths of the assistant
                                capabilities or the chat app's strengths?"
    user:                      "both"
    assistant [auto->clarify]: "Do you want strengths of the assistant, the
                                chat app, or both?"
    user:                      "both"
    assistant [auto->smart:planning]: answered.

The cause is the same one the Continue fix had: a follow-up whose meaning
depends on the previous assistant turn, routed as a standalone request. See
app/followup.py — this module tests the clarify half of it, and both halves of
the fix, which are independently necessary:

  1. the routing question is the ORIGINAL request recombined with the reply,
     because "both" alone carries nothing routable and always will;
  2. `allow_clarify=False`, because a recombined request can still read as
     ambiguous and a second clarify in a row must be impossible rather than
     unlikely.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages as messages_module
from app import database, followup
from app.schemas import AskRequest, AskResponse, Mode

# --- harness -------------------------------------------------------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def _seed_clarify_exchange(
    conversation_id: int,
    original: str = "what are your strengths",
    clarifying_question: str = (
        "Do you mean this assistant's strengths or the chat app's strengths?"
    ),
) -> None:
    """The state the loop starts from: an original request, and this app's own
    clarifying question persisted with mode_used="auto->clarify" exactly as
    decide_route/orchestrator write it."""
    database.add_message(conversation_id, "user", original)
    database.add_message(
        conversation_id,
        "assistant",
        clarifying_question,
        mode_used="auto->clarify",
    )


def _capture_routing(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what the router was asked to route, and under what guard."""
    calls: list[dict[str, Any]] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        recall_library: bool = False,
        memory_sources: list[dict[str, Any]] | None = None,
        forced_category: str | None = None,
        allow_auto_workflow: bool = True,
        allow_clarify: bool = True,
        require_code_execution: bool = False,
    ) -> AskResponse:
        calls.append(
            {
                "routing_question": routing_question,
                "allow_clarify": allow_clarify,
                "history": history,
            }
        )
        return AskResponse(
            answer="an answer", mode_used="auto->smart:planning", notes="n"
        )

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run_orchestrator)
    return calls


# --- followup.clarify_followup / last_assistant_was_clarify --------------------


def test_a_clarify_answer_is_recognised_from_the_previous_turns_mode_used(
    db_path,
) -> None:
    cid = int(database.create_conversation("t", None)["id"])
    _seed_clarify_exchange(cid)
    prior = database.list_messages(cid)

    assert followup.last_assistant_was_clarify(prior) is True


@pytest.mark.parametrize(
    ("mode_used", "role"),
    [
        # An ordinary answer is not a clarify, however it was routed.
        ("auto->smart:planning", "assistant"),
        ("auto->fast:coding", "assistant"),
        (None, "assistant"),
        # A user turn is never a clarify, whatever else is true.
        ("auto->clarify", "user"),
    ],
)
def test_an_ordinary_turn_is_not_treated_as_a_clarify(
    db_path, mode_used: str | None, role: str
) -> None:
    cid = int(database.create_conversation("t", None)["id"])
    database.add_message(cid, "user", "the original")
    database.add_message(cid, role, "something", mode_used=mode_used)
    prior = database.list_messages(cid)

    assert followup.last_assistant_was_clarify(prior) is False
    assert followup.clarify_followup(prior, "both") is None


def test_an_already_answered_clarify_does_not_capture_a_later_turn(db_path) -> None:
    """Only the IMMEDIATELY preceding message counts. A clarify from three
    turns back is finished business, and recombining a later unrelated request
    with it would merge two different questions."""
    cid = int(database.create_conversation("t", None)["id"])
    _seed_clarify_exchange(cid)
    database.add_message(cid, "user", "this assistant's strengths")
    database.add_message(cid, "assistant", "answered", mode_used="auto->smart:planning")
    prior = database.list_messages(cid)

    assert followup.last_assistant_was_clarify(prior) is False
    assert followup.clarify_followup(prior, "now something else") is None


# --- the routing question is the ORIGINAL request, not the reply ---------------


def test_the_combined_intent_routes_on_the_original_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heart of it: `both` carries no category, no complexity, no subject.
    The original request is where the routable content is, so the router must be
    given that, with the reply attached rather than in place of it."""
    calls = _capture_routing(monkeypatch)
    cid = _create(client)
    _seed_clarify_exchange(cid, original="design me a caching strategy")

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "both"})

    assert len(calls) == 1
    routed = calls[0]["routing_question"]
    # The original request's own words reach the classifier...
    assert "design me a caching strategy" in routed
    # ...and the reply is present as an answer to the question we asked, not as
    # the thing being classified on its own.
    assert "both" in routed
    assert routed.strip() != "both"
    assert (
        "Do you mean this assistant's strengths or the chat app's strengths?" in routed
    )


def test_an_ordinary_turn_still_routes_on_itself(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse, so the fix cannot quietly start recombining every turn: a
    turn that is not answering a clarify routes on exactly its own text, and may
    still clarify."""
    calls = _capture_routing(monkeypatch)
    cid = _create(client)
    database.add_message(cid, "user", "an earlier question")
    database.add_message(cid, "assistant", "an earlier answer", mode_used="auto->fast")

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "what about this"})

    assert calls[0]["routing_question"] == "what about this"
    assert calls[0]["allow_clarify"] is True


# --- the guard: never two clarifies in a row ------------------------------------


def test_a_reply_to_a_clarifying_question_forbids_another_clarify(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_routing(monkeypatch)
    cid = _create(client)
    _seed_clarify_exchange(cid)

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "both"})

    assert calls[0]["allow_clarify"] is False


@pytest.mark.parametrize(
    "reply",
    [
        "both",  # the observed one
        "it",  # a bare pronoun: maximally ambiguous by the classifier's own rule
        "that one",
        "the first",
        "",  # nothing at all
    ],
)
def test_a_reply_to_a_clarifying_question_never_produces_another_clarify(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    """The end-to-end property, through the REAL router: whatever the reply
    looks like, the answer to it is not another clarifying question. Driven by a
    classifier stub that reports `ambiguous` for everything — the worst case,
    and exactly what the live loop looked like."""
    _stub_always_ambiguous(monkeypatch)
    cid = _create(client)
    _seed_clarify_exchange(cid)

    body = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": reply or "both"}
    ).json()

    assert body["mode_used"] != "auto->clarify", (
        "the router asked a second clarifying question in a row"
    )
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    clarifies = [m for m in messages if m["mode_used"] == "auto->clarify"]
    assert len(clarifies) == 1, "more than one clarify is in this conversation"


def test_a_first_ambiguous_request_can_still_clarify(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not disable clarification generally — it is a recursion
    guard, not a preference. A genuinely ambiguous FIRST request still gets
    asked about, which is the behaviour the feature exists for."""
    _stub_always_ambiguous(monkeypatch)
    cid = _create(client)
    database.add_message(cid, "user", "tell me about the app")
    database.add_message(cid, "assistant", "an earlier answer", mode_used="auto->fast")

    body = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "improve it"}
    ).json()

    assert body["mode_used"] == "auto->clarify"


def _stub_always_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    """A classifier that flags EVERY request as ambiguous, so the only thing
    that can stop a second clarify is the guard itself."""
    from app import routing

    def fake_classify(
        question: str,
        client: object,
        overrides: dict[str, str] | None = None,
        history: str = "",
    ) -> dict[str, Any]:
        return {
            "category": "planning",
            "complexity": "high",
            "reason": "stub",
            "needs_live_data": False,
            "ambiguous": True,
            "clarifying_question": "Do you mean A or B?",
            "multi_part": False,
            "deliverables": 0,
        }

    monkeypatch.setattr(routing, "_classify_with_ai", fake_classify)
    # A client object must exist for decide_route to reach the classifier at
    # all; the stub above replaces every use of it.
    monkeypatch.setattr(
        "app.orchestrator_calls.get_client", lambda: object(), raising=False
    )


# --- decide_route's guard, directly ---------------------------------------------


def test_decide_route_honours_allow_clarify(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import routing

    _stub_always_ambiguous(monkeypatch)

    allowed = routing.decide_route(
        "improve it", Mode.auto, client=object(), history="ASSISTANT: A or B?"
    )
    assert allowed.ambiguous is True
    assert allowed.mode_used == "auto->clarify"

    forbidden = routing.decide_route(
        "both",
        Mode.auto,
        client=object(),
        history="ASSISTANT: A or B?",
        allow_clarify=False,
    )
    assert forbidden.ambiguous is False
    assert forbidden.mode_used != "auto->clarify"
    assert forbidden.clarifying_question == ""
    # ...and it is a real, dispatchable decision rather than the clarify
    # decision's never-dispatched placeholder (max_output_tokens=0).
    assert forbidden.max_output_tokens > 0


def test_a_suppressed_clarify_still_routes_on_the_classifiers_own_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Pick the most likely reading and proceed" means the category the
    classifier already supplied alongside its ambiguity verdict — not a
    fallback tier that discards it."""
    from app import routing

    _stub_always_ambiguous(monkeypatch)

    decision = routing.decide_route(
        "both",
        Mode.auto,
        client=object(),
        history="ASSISTANT: A or B?",
        allow_clarify=False,
    )

    assert decision.category == "planning"
    assert decision.mode_used.startswith("auto->")


# --- anti-drift ----------------------------------------------------------------


def test_the_guard_cannot_be_silently_removed() -> None:
    """The guard is one `not allow_clarify` away from being deleted by someone
    tidying, and its absence is invisible: the loop only shows up live, in a
    conversation, against a real classifier. So assert the wiring exists at
    every link in the chain rather than only its behaviour."""
    import inspect

    from app import orchestrator, routing
    from app.routers.messages import _shared, ask

    assert "allow_clarify" in inspect.signature(routing.decide_route).parameters
    assert (
        "allow_clarify" in inspect.signature(orchestrator.run_orchestrator).parameters
    )
    assert (
        "allow_clarify"
        in inspect.signature(orchestrator.stream_orchestrator).parameters
    )
    assert "allow_clarify" in inspect.signature(_shared._stream_and_persist).parameters
    # The clarify branch must be conditional on the guard, not unconditional.
    source = inspect.getsource(routing.decide_route)
    assert "not allow_clarify" in source, (
        "decide_route no longer honours allow_clarify; the clarify loop is back"
    )
    # Both ask paths must derive their routing question through the follow-up
    # helper rather than passing the raw turn.
    ask_source = inspect.getsource(ask)
    assert ask_source.count("_followup_routing(prior_messages, req.question)") == 2, (
        "an ask path stopped routing clarify answers through _followup_routing"
    )


def test_resume_route_lives_in_one_place_only() -> None:
    """The Continue half of the same mechanism. It was moved into app/followup.py
    so both cases share one home; a second copy in the route module is how this
    class of bug got to five instances."""
    import inspect

    from app.routers.messages import ask

    assert not hasattr(ask, "_resume_route"), (
        "ask.py has its own _resume_route again; use followup.resume_route"
    )
    assert "def resume_route" in inspect.getsource(followup)


def test_a_clarify_with_no_user_turn_behind_it_has_nothing_to_recombine(
    db_path,
) -> None:
    """Not reachable in practice — the classifier needs history to find an
    ambiguous reference at all — but if it happens there is no original request
    to recombine, so routing falls back to the reply and the guard alone stops
    the loop."""
    cid = int(database.create_conversation("t", None)["id"])
    database.add_message(
        cid, "assistant", "Do you mean A or B?", mode_used="auto->clarify"
    )
    prior = database.list_messages(cid)

    assert followup.last_assistant_was_clarify(prior) is True
    assert followup.clarify_followup(prior, "both") is None
