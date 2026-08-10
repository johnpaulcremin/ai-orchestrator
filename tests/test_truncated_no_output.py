"""A call cut off before it wrote anything must explain itself, not vanish.

Output tokens are spent on a hosted tool call's arguments (the Python of a
code-execution block) and a reasoning model's private thinking before any
visible text exists. A large enough one exhausts the ceiling while the answer
is still empty — the model is cut off mid-`tool_use`, the tool never runs, and
the call is billed in full. The empty answer is then (correctly) refused by
the persistence guards in tests/test_empty_answer_guards.py, leaving the user
with "this question didn't get an answer": no cause, and no cue that retrying
verbatim will fail identically.

Observed live on "Make the spreadsheet as per your description": five
consecutive smart-tier calls, each landing on exactly 4000 output tokens,
~$0.47 billed for zero output.

Two halves:
  * the ceiling — an ordinary ask that wants a FILE now gets the artefact
    ceiling a workflow step already got (app/orchestrator_tools.py's
    _looks_like_artefact_request), so the truncation is far less likely;
  * the message — when it happens anyway the answer says so, and persists
    carrying `truncated`/`max_output_tokens`/`no_output`, which is what makes
    the existing ceiling notice and "Retry as workflow" apply to it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import cache
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.orchestrator_tools import _looks_like_artefact_request
from app.schemas import AskRequest, Mode


def _truncating_call(text: str = "") -> Any:
    def fake_call_model(**kwargs: Any) -> str:
        truncated = kwargs["truncated"]
        if truncated is not None:
            truncated.append(True)
        return text

    return fake_call_model


def _truncating_stream(chunks: list[str] | None = None) -> Any:
    def fake_stream_model(**kwargs: Any):
        truncated = kwargs["truncated"]
        if truncated is not None:
            truncated.append(True)
        yield from chunks or []

    return fake_stream_model


def _done(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(e["data"] for e in events if e["event"] == "done")


# --- the artefact heuristic ---------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Can you put this all neatly into an Excel document?",
        "make the spreadsheet as per your description",
        "give me a csv file of the results",
        "export it as a file",
        "build me an xlsx",
    ],
)
def test_artefact_requests_are_recognised(question: str) -> None:
    assert _looks_like_artefact_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "summarise this conversation",
        "how is this app better than others?",
        "explain how excel formulas work",  # about spreadsheets, not asking for one
        "",
    ],
)
def test_prose_requests_are_not_mistaken_for_artefacts(question: str) -> None:
    assert _looks_like_artefact_request(question) is False


def test_an_ordinary_artefact_ask_gets_the_artefact_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap this closes: `require_code_execution` comes from a workflow
    planner, so an ordinary ask never reached the raise and kept the smart
    tier's prose-sized 4000."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, Any] = {}

    def fake_call_model(**kwargs: Any) -> str:
        seen["ceiling"] = kwargs["max_output_tokens"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(
        AskRequest(question="put this into an Excel document", mode=Mode.smart)
    )

    assert seen["ceiling"] == 8000


def test_a_prose_ask_keeps_its_tier_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heuristic must not inflate every ask: the raise costs a bigger
    budget reservation, and prose does not need it."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, Any] = {}

    def fake_call_model(**kwargs: Any) -> str:
        seen["ceiling"] = kwargs["max_output_tokens"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="summarise the cons", mode=Mode.smart))

    assert seen["ceiling"] == 4000


def test_a_workflow_step_never_consults_the_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow step already has its planner's verdict, and its prompts quote
    the original request — so the heuristic would see "spreadsheet" in a
    SYNTHESIS prompt and promote a step meant for the cheap lane onto a
    code-capable model. `forced_category` marks a step; the heuristic stands
    down for one."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, Any] = {}

    def fake_call_model(**kwargs: Any) -> str:
        seen["ceiling"] = kwargs["max_output_tokens"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(
        AskRequest(question="combine the summary and the spreadsheet", mode=Mode.smart),
        forced_category="summarization",
    )

    assert seen["ceiling"] == 4000  # not raised


# --- the message -------------------------------------------------------------


def test_truncated_empty_answer_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call())

    result = run_orchestrator(AskRequest(question="q", mode=Mode.smart))

    # NOT "" — the guards would have dropped that on the floor.
    assert "ran out of output space" in result.answer
    assert result.truncated is True
    assert result.no_output is True
    # The ceiling rides along, so the existing notice can name it.
    assert result.max_output_tokens


def test_the_note_does_not_restate_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI's truncation notice already names it from max_output_tokens.
    Saying it twice, from a second source that can drift, is worse than once."""
    monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call())

    answer = run_orchestrator(AskRequest(question="q", mode=Mode.smart)).answer
    assert "4,000" not in answer and "4000" not in answer


def test_a_truncation_that_produced_text_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That case has something to resume, so Continue still applies and an
    appended apology would just be noise."""
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call("half an answer"))

    result = run_orchestrator(AskRequest(question="q", mode=Mode.smart))
    assert result.answer == "half an answer"
    assert result.no_output is False


def test_an_empty_answer_without_truncation_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note is specifically about hitting the ceiling; every other empty
    answer keeps the existing semantics rather than a wrong explanation."""
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "")

    assert run_orchestrator(AskRequest(question="q", mode=Mode.smart)).answer == ""


def test_stream_truncated_empty_answer_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", _truncating_stream())

    events = list(stream_orchestrator(AskRequest(question="q", mode=Mode.smart)))
    done = _done(events)

    assert "ran out of output space" in str(done["answer"])
    assert done["no_output"] is True
    # Streamed as a delta too, so a waiting UI resolves into the explanation.
    deltas = "".join(str(e["data"]["text"]) for e in events if e["event"] == "delta")
    assert "ran out of output space" in deltas


def test_stream_truncation_with_text_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", _truncating_stream(["partial"]))

    done = _done(list(stream_orchestrator(AskRequest(question="q", mode=Mode.smart))))
    assert done["answer"] == "partial"
    assert done["no_output"] is False


# --- it persists, and Continue is refused for it ------------------------------


def test_the_explanation_is_persisted_as_a_real_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call())

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "a spreadsheet", "mode": "smart"},
    )

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["no_output"] is True
    assert messages[1]["truncated"] is True


def test_continue_is_refused_for_an_answer_with_nothing_to_resume(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continuing would bill a call to resume the app's own apology."""
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call())

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "a spreadsheet", "mode": "smart"},
    )
    message_id = client.get(f"/v1/conversations/{cid}/messages").json()[1]["id"]

    res = client.post(f"/v1/conversations/{cid}/messages/{message_id}/continue")
    assert res.status_code == 400
    assert "nothing to continue" in res.json()["detail"]


# --- a cut-off answer is never cached ----------------------------------------


def test_a_truncated_answer_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freezing an incomplete answer in would replay it — or the bare "I ran
    out of output space" note — to every later asker, for free and forever."""
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", _truncating_call("half an answer"))

    req = AskRequest(question="a distinctly cacheable question", mode=Mode.smart)
    run_orchestrator(req)

    assert cache.get(orchestrator._cache_key(req.question, req.mode.value)) is None
