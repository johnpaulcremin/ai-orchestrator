"""Parity between the ask paths: one response builder, one persister.

There used to be two hand-written AskResponse builders in
routers/messages/ask.py -- one for mode="workflow", one for everything else --
kept in step by whoever remembered. Three fields were lost that way, and every
one was found in production rather than here:

* `workflow_steps` and `failure_message` were absent from the ORDINARY builder,
  which is what an auto-routed workflow returns through (the routing decision is
  made inside the orchestrator, after the router layer has already chosen a
  builder). Live runs came back `auto->workflow(5 steps)` with an empty
  breakdown, and a stopped step's plain-English message never reached the user.
* `model` was absent from the WORKFLOW branch, so an explicit mode="workflow"
  answer rendered no model badge -- and its streaming twin persisted no model
  either, so the badge appeared during the answer and vanished on reload.

Each was invisible for the same reason: a dropped field reads as absent, which
is indistinguishable from "this path has none of those".

So this module asserts PARITY, not just correctness:

1. The eight fields only the ordinary path ever populates keep their exact
   on-the-wire values. A field that is None on a path stays None -- never
   normalised to [] or {} while tidying, which would be a silent API change for
   every client that checks truthiness.
2. Each path's response AND persisted row carry what that path is supposed to
   carry, checked field by field rather than by shape.
3. _api_response cannot drop a field by omission, for any future field.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages as messages_module
from app.routers.messages.ask import _api_response
from app.schemas import (
    AcademicResult,
    AskRequest,
    AskResponse,
    FactCheck,
    LibrarySource,
    MathResult,
    MemorySource,
    PendingAction,
    Source,
    WorkflowStep,
)

# The fields ONLY an ordinary answer ever populates. Every one must stay
# untouched by the consolidation -- absent where it was absent.
ORDINARY_ONLY_FIELDS = (
    "sources",
    "search_queries",
    "pending_action",
    "fact_checks",
    "academic_results",
    "math_results",
    "library_sources",
    "memory_sources",
)


def _rich_ordinary() -> AskResponse:
    """An ordinary answer with every optional field populated, so the eight
    fields above are non-empty on this path and a regression that blanked them
    could not pass."""
    return AskResponse(
        answer="an ordinary answer",
        mode_used="auto->smart:analysis",
        notes="ordinary notes",
        model="gpt-5",
        input_tokens=11,
        output_tokens=22,
        cost_usd=0.5,
        truncated=True,
        sources=[Source(title="t", url="https://example.com/a")],
        search_queries=["a query"],
        pending_action=PendingAction(action="notify", summary="s", payload={"k": "v"}),
        images=["data:image/png;base64,ZmFrZQ=="],
        code_results=[{"code": "print(1)", "logs": "1", "images": [], "files": []}],
        fact_checks=[
            FactCheck(
                claim="c", rating="True", publisher="P", url="https://example.com/f"
            )
        ],
        academic_results=[
            AcademicResult(
                title="paper",
                authors="A",
                year=2020,
                venue="V",
                citation_count=3,
                url="https://example.com/p",
                abstract_snippet="s",
            )
        ],
        math_results=[
            MathResult(operation="solve", expression="x-1", variable="x", result="x=1")
        ],
        library_sources=[LibrarySource(document="d.pdf", snippet_count=2)],
        memory_sources=[
            MemorySource(conversation_title="prev", created_at="2026-01-01T00:00:00Z")
        ],
    )


def _workflow_result(mode_used: str) -> AskResponse:
    """Exactly the shape run_workflow returns: the workflow fields set, the
    eight ordinary-only fields never touched -- so None, not []."""
    return AskResponse(
        answer="a workflow answer",
        mode_used=mode_used,
        notes="Workflow: 3 step(s) (2 + synthesis)",
        failure_message="One step of this workflow was skipped.",
        model="claude-sonnet-5",
        input_tokens=33,
        output_tokens=44,
        cost_usd=1.5,
        workflow_steps=[
            WorkflowStep(
                category="analysis", instruction="summarise", model="gpt-5", status="ok"
            ),
            WorkflowStep(
                category="analysis", instruction="chart", model="", status="failed"
            ),
        ],
        code_results=[
            {
                "code": "df.to_csv('x.csv')",
                "logs": "ok",
                "images": ["data:image/png;base64,Y2hhcnQ="],
                "files": [
                    {
                        "filename": "x.csv",
                        "mime_type": "text/csv",
                        "data": "data:text/csv;base64,YSxiCjEsMgo=",
                    }
                ],
            }
        ],
        images=["data:image/png;base64,d2Y="],
    )


def _ask(client: TestClient, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask once and return (response body, persisted assistant message)."""
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    body = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "q", "mode": mode}
    ).json()
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    row = next(m for m in messages if m["role"] == "assistant")
    return body, row


# --- constraint 1: the eight fields keep their exact values --------------------


def test_an_ordinary_answer_still_carries_all_eight_of_its_own_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    body, row = _ask(client, "auto")

    for field in ORDINARY_ONLY_FIELDS:
        assert body[field], f"{field} was lost from the response"
        assert row[field], f"{field} was lost from the persisted row"
    # The one field derived from another: an action implies a pending status.
    assert row["action_status"] == "pending"


@pytest.mark.parametrize(
    "mode", ["auto", "workflow"], ids=["auto_workflow", "explicit"]
)
def test_a_workflow_answer_leaves_the_eight_fields_absent_not_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The constraint that makes the consolidation safe to ship. A workflow has
    no sources, no pending action and no fact checks, and it must keep saying so
    with None -- normalising to [] or {} while tidying would silently change the
    API for every client that checks truthiness."""
    result = _workflow_result(
        "auto->workflow(3 steps)" if mode == "auto" else "workflow(3 steps)"
    )
    monkeypatch.setattr(messages_module, "run_orchestrator", lambda req, **k: result)
    monkeypatch.setattr(messages_module, "run_workflow", lambda req, **k: result)

    body, row = _ask(client, mode)

    for field in ORDINARY_ONLY_FIELDS:
        assert body[field] is None, f"{field} became {body[field]!r} in the response"
        assert row[field] is None, f"{field} became {row[field]!r} in the persisted row"
    assert row["action_status"] is None


# --- constraint 2: each path carries what it should, field by field -----------


@pytest.mark.parametrize(
    "mode", ["auto", "workflow"], ids=["auto_workflow", "explicit"]
)
def test_every_workflow_path_carries_model_steps_and_failure_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """All three of the fields that were lost, on both non-streaming workflow
    paths. `model` on mode="workflow" is the fix; the other two were fixed a
    commit earlier on the auto-routed path."""
    result = _workflow_result(
        "auto->workflow(3 steps)" if mode == "auto" else "workflow(3 steps)"
    )
    monkeypatch.setattr(messages_module, "run_orchestrator", lambda req, **k: result)
    monkeypatch.setattr(messages_module, "run_workflow", lambda req, **k: result)

    body, row = _ask(client, mode)

    assert body["model"] == "claude-sonnet-5", "the model badge would be blank"
    assert row["model"] == "claude-sonnet-5", "the badge would vanish on reload"
    assert [s["status"] for s in body["workflow_steps"]] == ["ok", "failed"]
    assert [s["status"] for s in row["workflow_steps"]] == ["ok", "failed"]
    assert body["failure_message"] == "One step of this workflow was skipped."
    # ...and the artefacts a workflow exists to deliver.
    assert [f["filename"] for cr in body["code_results"] for f in cr["files"]] == [
        "x.csv"
    ]
    assert body["images"] == ["data:image/png;base64,d2Y="]


def test_an_ordinary_answer_reports_no_workflow_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse: sharing one builder must not invent workflow fields for an
    answer that never was a workflow."""
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    body, row = _ask(client, "auto")

    assert body["workflow_steps"] is None
    assert row["workflow_steps"] is None
    assert body["failure_message"] is None


def test_notes_gets_exactly_one_context_messages_suffix_on_both_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single override the shared builder applies. Both hand-written
    builders appended this; a consolidation that dropped or doubled it would
    change every answer's diagnostics."""
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    # 0, not 1: prior_messages is read before this turn's own user message is
    # saved, so a fresh conversation reports no prior context.
    body, _ = _ask(client, "auto")
    assert body["notes"] == "ordinary notes | context_messages=0"

    wf = _workflow_result("workflow(3 steps)")
    monkeypatch.setattr(messages_module, "run_workflow", lambda req, **k: wf)
    body, _ = _ask(client, "workflow")
    assert body["notes"].count("context_messages=") == 1
    assert body["notes"].endswith("| context_messages=0")


# --- constraint 3: the builder cannot drop a field by omission ----------------


def test_the_shared_builder_carries_every_field_it_is_given() -> None:
    """The anti-drift assertion, and the reason the builder is a copy rather
    than a field list. Whatever the orchestrator or the workflow set must come
    out the other side, including any field added to AskResponse later -- which
    is precisely how `model`, `workflow_steps` and `failure_message` were each
    lost from a hand-maintained list."""
    source = _rich_ordinary().model_copy(
        update={
            "failure_message": "a failure",
            "workflow_steps": [
                WorkflowStep(
                    category="analysis", instruction="i", model="m", status="ok"
                )
            ],
            "cached": True,
        }
    )

    built = _api_response(source, "context_messages=3")

    for field in AskResponse.model_fields:
        if field == "notes":
            continue
        assert getattr(built, field) == getattr(source, field), (
            f"{field} was dropped or altered by the shared builder"
        )
    assert built.notes == "ordinary notes | context_messages=3"

    # And no field of AskResponse is left unaccounted for by this check.
    assert "notes" in AskResponse.model_fields
    assert len(AskResponse.model_fields) > 15, "field list shrank unexpectedly"


# --- streaming: the badge must survive a reload -------------------------------


def _sse(client: TestClient, mode: str) -> tuple[str, dict[str, Any]]:
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    with client.stream(
        "POST",
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "q", "mode": mode},
    ) as response:
        frames = "".join(response.iter_text())
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    row = next(m for m in messages if m["role"] == "assistant")
    return frames, row


def test_a_streamed_workflow_persists_the_model_it_streamed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nastier half of the `model` gap: the "done" event has always carried
    the model, so the client rendered the badge live and then lost it on reload
    -- which reads as data loss rather than a missing feature."""
    result = _workflow_result("workflow(3 steps)")

    def fake_stream(*args: object, **kwargs: object):
        yield {"event": "delta", "data": {"text": result.answer}}
        yield {
            "event": "done",
            "data": {
                "answer": result.answer,
                "mode_used": result.mode_used,
                "notes": result.notes,
                "model": result.model,
                "workflow_steps": [s.model_dump() for s in result.workflow_steps or []],
            },
        }

    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream)
    frames, row = _sse(client, "workflow")

    assert "claude-sonnet-5" in frames, "the model never reached the client"
    assert row["model"] == "claude-sonnet-5", (
        "the badge would show while streaming and vanish on reload"
    )
    assert [s["status"] for s in row["workflow_steps"]] == ["ok", "failed"]


def test_a_streamed_ordinary_answer_still_persists_its_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This worker already did the right thing; pin it so the fix above cannot
    be 'tidied' by making the two workers match in the wrong direction."""

    def fake_stream(*args: object, **kwargs: object):
        yield {"event": "delta", "data": {"text": "hello"}}
        yield {
            "event": "done",
            "data": {
                "answer": "hello",
                "mode_used": "auto->fast",
                "notes": "n",
                "model": "gpt-5-mini",
            },
        }

    monkeypatch.setattr(messages_module, "stream_orchestrator", fake_stream)
    _, row = _sse(client, "auto")
    assert row["model"] == "gpt-5-mini"


def test_ask_request_and_response_agree_on_what_a_workflow_step_looks_like() -> None:
    """Cheap guard on the shape the parity tests above assume."""
    step = WorkflowStep(
        category="analysis", instruction="i", model="m", status="failed"
    )
    assert step.model_dump()["status"] == "failed"
    assert AskRequest(question="q").mode.value == "auto"


# --- constraint 4: the SAME parity on the retry paths --------------------------
#
# ba15508 consolidated ask.py's two builders and claimed the loss-by-omission
# class was structurally impossible. It was not: regenerate.py and edit.py kept
# hand-written builders of their own, outside the blast radius of that change,
# and each dropped FIVE AskResponse fields — search_queries, library_sources,
# memory_sources, workflow_steps and failure_message. Those were the fourth and
# fifth instances of the same bug. The helpers now live in _shared.py and all
# three route families call them; these tests are what makes that checkable
# rather than asserted in a docstring.

RETRY_PATHS = ("regenerate", "edit")


def _retry(
    client: TestClient, path: str, mode: str = "auto"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask once, then retry that turn via `path`, and return (response body,
    persisted assistant row) for the RETRY."""
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    if path == "regenerate":
        body = client.post(
            f"/v1/conversations/{cid}/regenerate", json={"mode": mode}
        ).json()
    else:
        user_id = next(m for m in messages if m["role"] == "user")["id"]
        body = client.post(
            f"/v1/conversations/{cid}/messages/{user_id}/edit",
            json={"question": "q edited", "mode": mode},
        ).json()
    rows = client.get(f"/v1/conversations/{cid}/messages").json()
    row = next(m for m in rows if m["role"] == "assistant")
    return body, row


@pytest.mark.parametrize("path", RETRY_PATHS)
def test_a_retry_carries_all_eight_ordinary_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """search_queries, library_sources and memory_sources were three of the five
    fields these two paths dropped — invisible exactly like the others, since a
    dropped field reads as "this answer had none"."""
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    body, row = _retry(client, path)

    for field in ORDINARY_ONLY_FIELDS:
        assert body[field], f"{field} was lost from the {path} response"
        assert row[field], f"{field} was lost from the persisted {path} row"
    assert row["action_status"] == "pending"


@pytest.mark.parametrize("path", RETRY_PATHS)
def test_an_auto_routed_workflow_retry_keeps_its_breakdown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The fourth and fifth instances themselves: AUTO_WORKFLOW can route a
    retry into a workflow inside the orchestrator, and both retry paths dropped
    the per-step breakdown and the failure message on the way out."""
    result = _workflow_result("auto->workflow(3 steps)")
    monkeypatch.setattr(messages_module, "run_orchestrator", lambda req, **k: result)

    body, row = _retry(client, path)

    assert [s["status"] for s in body["workflow_steps"]] == ["ok", "failed"]
    assert [s["status"] for s in row["workflow_steps"]] == ["ok", "failed"]
    assert body["failure_message"] == "One step of this workflow was skipped."
    assert body["model"] == "claude-sonnet-5"
    assert row["model"] == "claude-sonnet-5"


@pytest.mark.parametrize("path", RETRY_PATHS)
def test_a_retry_reports_no_workflow_fields_for_an_ordinary_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    body, row = _retry(client, path)

    assert body["workflow_steps"] is None
    assert row["workflow_steps"] is None
    assert body["failure_message"] is None


@pytest.mark.parametrize("path", RETRY_PATHS)
def test_a_retry_keeps_its_own_notes_suffix(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Sharing ask's builder must not give a retry ask's notes: each path's own
    marker ("regenerated"/"edited") is how a persisted answer says which control
    produced it."""
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _rich_ordinary()
    )
    body, _ = _retry(client, path)

    marker = "regenerated" if path == "regenerate" else "edited"
    # 0, not 1: `prior` excludes the turn being retried itself, so a retry of
    # the first turn reports no prior context — the same string these paths
    # produced before they shared ask's builder, byte for byte.
    assert body["notes"].endswith(f"| {marker} | context_messages=0")
    assert body["notes"].count("context_messages=") == 1


def test_no_answer_path_hand_builds_an_ask_response() -> None:
    """The structural guard the original consolidation lacked. A new hand-written
    AskResponse(...) in any of these three modules is how the fourth and fifth
    instances happened, and is the one thing a behavioural test cannot notice —
    it would simply drop whichever field the author forgot, on a path no test
    covers yet."""
    import pathlib

    root = pathlib.Path(messages_module.__file__).parent
    for name in ("ask.py", "regenerate.py", "edit.py"):
        source = (root / name).read_text()
        assert "AskResponse(" not in source, (
            f"{name} constructs an AskResponse directly; use _shared._api_response "
            "so a field cannot be dropped by omission"
        )
