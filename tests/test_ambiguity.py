"""Ambiguity-triggered clarifying questions.

The router's existing classifier call (already made once per "auto" request)
is extended to also flag when a message references something ambiguous in
recent history (e.g. "this"/"it" could mean either of two things just
discussed) and, when so, the orchestrator returns a clarifying question
directly instead of calling any fast/smart model — cheaper than guessing
wrong and burning a full answer on the wrong interpretation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import orchestrator
from app.routers.messages import build_recent_history_snippet
from app.routing import RouteDecision, _parse_classifier_json, decide_route
from app.schemas import AskRequest, Mode


class FakeClassifierClient:
    def __init__(self, output_text: str) -> None:
        result = SimpleNamespace(output_text=output_text)
        self.responses = SimpleNamespace(create=lambda **kwargs: result)

    def with_options(self, **kwargs: object) -> "FakeClassifierClient":
        return self


_AMBIGUOUS_JSON = (
    '{"category": "casual_chat", "complexity": "low", "reason": "ref check", '
    '"needs_live_data": false, "ambiguous": true, '
    '"clarifying_question": "Do you mean the app, or me?"}'
)
_CLEAR_JSON = (
    '{"category": "casual_chat", "complexity": "low", "reason": "greeting", '
    '"needs_live_data": false, "ambiguous": false, "clarifying_question": ""}'
)


# --- build_recent_history_snippet ---------------------------------------------


def test_build_recent_history_snippet_formats_role_and_content() -> None:
    snippet = build_recent_history_snippet(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert snippet == "USER: hi\nASSISTANT: hello"


def test_build_recent_history_snippet_empty_for_no_history() -> None:
    assert build_recent_history_snippet([]) == ""


def test_build_recent_history_snippet_caps_turns_and_length() -> None:
    long_content = "x" * 1000
    messages = [{"role": "user", "content": f"turn {i}"} for i in range(10)]
    messages.append({"role": "user", "content": long_content})

    snippet = build_recent_history_snippet(messages, turns=4)

    assert snippet.count("\n") == 3  # 4 turns -> 3 newlines
    assert "turn 0" not in snippet  # only the last 4 turns kept
    assert len(snippet) < len(long_content)  # each line capped


# --- _parse_classifier_json: ambiguous/clarifying_question ---------------------


def test_parse_classifier_json_reads_ambiguous_fields() -> None:
    parsed = _parse_classifier_json(_AMBIGUOUS_JSON)
    assert parsed is not None
    assert parsed["ambiguous"] is True
    assert parsed["clarifying_question"] == "Do you mean the app, or me?"


def test_parse_classifier_json_treats_ambiguous_with_no_question_as_not_ambiguous() -> (
    None
):
    raw = (
        '{"category": "casual_chat", "complexity": "low", "reason": "r", '
        '"needs_live_data": false, "ambiguous": true, "clarifying_question": ""}'
    )
    parsed = _parse_classifier_json(raw)
    assert parsed is not None
    assert parsed["ambiguous"] is False
    assert parsed["clarifying_question"] == ""


def test_parse_classifier_json_defaults_ambiguous_false_when_absent() -> None:
    raw = '{"category": "coding", "complexity": "high", "reason": "r"}'
    parsed = _parse_classifier_json(raw)
    assert parsed is not None
    assert parsed["ambiguous"] is False


# --- decide_route ---------------------------------------------------------------


def test_decide_route_returns_ambiguous_decision() -> None:
    client = FakeClassifierClient(_AMBIGUOUS_JSON)
    decision = decide_route(
        "what's special about this",
        Mode.auto,
        client=client,
        history="USER: tell me about this app\nASSISTANT: sure, here's a framework",
    )
    assert decision.ambiguous is True
    assert decision.clarifying_question == "Do you mean the app, or me?"


def test_decide_route_not_ambiguous_when_classifier_says_so() -> None:
    client = FakeClassifierClient(_CLEAR_JSON)
    decision = decide_route("hi there", Mode.auto, client=client, history="")
    assert decision.ambiguous is False
    assert decision.clarifying_question == ""


def test_decide_route_fast_mode_never_ambiguous_even_with_history() -> None:
    # Explicit modes bypass the classifier entirely — ambiguity detection is
    # an auto-mode-only concept.
    client = FakeClassifierClient(_AMBIGUOUS_JSON)
    decision = decide_route(
        "what's special about this", Mode.fast, client=client, history="some history"
    )
    assert decision.ambiguous is False


# --- run_orchestrator / stream_orchestrator short-circuit -----------------------


def test_run_orchestrator_returns_clarifying_question_without_calling_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            model="fast-x",
            mode_used="auto->clarify",
            notes="ambiguous",
            max_output_tokens=0,
            reasoning_effort="minimal",
            ambiguous=True,
            clarifying_question="Do you mean the app, or me?",
        ),
    )

    def fail_if_called(**_kwargs: object) -> str:
        raise AssertionError("_call_model must not be called when ambiguous")

    monkeypatch.setattr(orchestrator, "_call_model", fail_if_called)

    result = orchestrator.run_orchestrator(
        AskRequest(question="what about this", mode=Mode.auto)
    )

    assert result.answer == "Do you mean the app, or me?"
    assert result.mode_used == "auto->clarify"


def test_stream_orchestrator_yields_clarifying_question_without_calling_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            model="fast-x",
            mode_used="auto->clarify",
            notes="ambiguous",
            max_output_tokens=0,
            reasoning_effort="minimal",
            ambiguous=True,
            clarifying_question="Do you mean the app, or me?",
        ),
    )

    def fail_if_called(**_kwargs: object):
        raise AssertionError("_stream_model must not be called when ambiguous")

    monkeypatch.setattr(orchestrator, "_stream_model", fail_if_called)

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question="what about this", mode=Mode.auto)
        )
    )

    assert events[0]["event"] == "meta"
    assert events[1] == {
        "event": "delta",
        "data": {"text": "Do you mean the app, or me?"},
    }
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["answer"] == "Do you mean the app, or me?"


# --- HTTP: end-to-end through /ask ----------------------------------------------


def _create_conversation(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_persists_clarifying_question_when_classifier_flags_ambiguous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orchestrator, "get_client", lambda: FakeClassifierClient(_AMBIGUOUS_JSON)
    )

    def fail_if_called(**_kwargs: object) -> str:
        raise AssertionError("a fast/smart model must not be called")

    monkeypatch.setattr(orchestrator, "_call_model", fail_if_called)

    cid = _create_conversation(client)
    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what's special about this", "mode": "auto"},
    )

    assert res.status_code == 200
    assert res.json()["answer"] == "Do you mean the app, or me?"

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Do you mean the app, or me?"
