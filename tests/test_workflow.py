"""Opt-in multi-step workflow mode (app/workflow.py; mode="workflow"): plan
parsing, the unparseable-plan fallback, per-step execution, atomic budget
reservation, SSE step events, and persistence — see the module docstring in
app/workflow.py for the overall design.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import budget, orchestrator, orchestrator_calls, providers, workflow
from app.schemas import AskRequest, AskResponse, Mode, WorkflowStep

# --- config parsing ------------------------------------------------------------


def test_max_steps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_MAX_STEPS", raising=False)
    assert workflow.max_steps() == 4


def test_max_steps_clamped_to_hard_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_MAX_STEPS", "99")
    assert workflow.max_steps() == 6


def test_max_steps_invalid_or_zero_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_MAX_STEPS", "0")
    assert workflow.max_steps() == 4
    monkeypatch.setenv("WORKFLOW_MAX_STEPS", "not-a-number")
    assert workflow.max_steps() == 4


def test_step_max_output_tokens_default_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKFLOW_STEP_MAX_OUTPUT_TOKENS", raising=False)
    assert workflow.step_max_output_tokens() == 1500
    monkeypatch.setenv("WORKFLOW_STEP_MAX_OUTPUT_TOKENS", "0")
    assert workflow.step_max_output_tokens() == 1500
    monkeypatch.setenv("WORKFLOW_STEP_MAX_OUTPUT_TOKENS", "500")
    assert workflow.step_max_output_tokens() == 500


# --- plan parsing ----------------------------------------------------------------


_VALID_PLAN = (
    '{"steps": ['
    '{"category": "coding", "instruction": "write the function"},'
    '{"category": "debugging", "instruction": "find the bug"}'
    '], "synthesis_instruction": "combine both"}'
)


def test_parse_plan_json_valid() -> None:
    plan = workflow._parse_plan_json(_VALID_PLAN, cap=4)
    # A plan that omits the artefact fields entirely (a model that rejected
    # the strict schema) still parses, defaulting to prose steps -- wrongly
    # marking a step as artefact-producing would force a pricier model.
    assert plan == {
        "steps": [
            {
                "category": "coding",
                "instruction": "write the function",
                "produces_artefact": False,
                "artefact": "",
                "inputs": [],
            },
            {
                "category": "debugging",
                "instruction": "find the bug",
                "produces_artefact": False,
                "artefact": "",
                "inputs": [],
            },
        ],
        "synthesis_instruction": "combine both",
    }


def test_parse_plan_json_strips_markdown_fence() -> None:
    fenced = f"```json\n{_VALID_PLAN}\n```"
    plan = workflow._parse_plan_json(fenced, cap=4)
    assert plan is not None
    assert len(plan["steps"]) == 2


def test_parse_plan_json_malformed_returns_none() -> None:
    assert workflow._parse_plan_json("not json at all", cap=4) is None
    assert workflow._parse_plan_json("", cap=4) is None
    assert workflow._parse_plan_json("{broken", cap=4) is None


def test_parse_plan_json_empty_steps_returns_none() -> None:
    assert (
        workflow._parse_plan_json('{"steps": [], "synthesis_instruction": "x"}', cap=4)
        is None
    )


def test_parse_plan_json_rejects_unknown_category() -> None:
    bad = '{"steps": [{"category": "not_a_real_category", "instruction": "x"}], "synthesis_instruction": "y"}'
    assert workflow._parse_plan_json(bad, cap=4) is None


def test_parse_plan_json_rejects_missing_instruction() -> None:
    bad = '{"steps": [{"category": "coding", "instruction": ""}], "synthesis_instruction": "y"}'
    assert workflow._parse_plan_json(bad, cap=4) is None


def test_parse_plan_json_oversized_plan_truncated_to_cap() -> None:
    steps = ",".join(
        f'{{"category": "coding", "instruction": "step {i}"}}' for i in range(10)
    )
    raw = f'{{"steps": [{steps}], "synthesis_instruction": "combine"}}'
    plan = workflow._parse_plan_json(raw, cap=3)
    assert plan is not None
    assert len(plan["steps"]) == 3


def test_parse_plan_json_missing_synthesis_instruction_defaults_empty() -> None:
    raw = '{"steps": [{"category": "coding", "instruction": "x"}]}'
    plan = workflow._parse_plan_json(raw, cap=4)
    assert plan is not None
    assert plan["synthesis_instruction"] == ""


# --- _plan_workflow: the actual "cheap call" plumbing ---------------------------


class _FakePlanClient:
    def __init__(self, output_text: str) -> None:
        result = SimpleNamespace(output_text=output_text)
        self.responses = SimpleNamespace(create=lambda **kwargs: result)

    def with_options(self, **kwargs: object) -> "_FakePlanClient":
        return self


class _RaisingPlanClient:
    def __init__(self) -> None:
        def _raise(**kwargs: object) -> object:
            raise RuntimeError("planner down")

        self.responses = SimpleNamespace(create=_raise)

    def with_options(self, **kwargs: object) -> "_RaisingPlanClient":
        return self


def test_plan_workflow_returns_parsed_plan_on_success() -> None:
    client = _FakePlanClient(_VALID_PLAN)
    plan = workflow._plan_workflow("do two things", client, {}, cap=4)
    assert plan is not None
    assert len(plan["steps"]) == 2


def test_plan_workflow_returns_none_on_failure() -> None:
    client = _RaisingPlanClient()
    assert workflow._plan_workflow("q", client, {}, cap=4) is None


def test_plan_workflow_returns_none_on_unparseable_output() -> None:
    client = _FakePlanClient("not json")
    assert workflow._plan_workflow("q", client, {}, cap=4) is None


# --- run_workflow: fallback path -------------------------------------------------


def test_run_workflow_falls_back_to_single_ask_on_unparseable_plan(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: None)

    captured: dict[str, object] = {}

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        captured["mode"] = req.mode
        captured["question"] = req.question
        return AskResponse(answer="fallback answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)

    result = workflow.run_workflow(AskRequest(question="a simple question"))
    assert result.answer == "fallback answer"
    assert captured["mode"] == Mode.auto  # never left as workflow
    assert captured["question"] == "a simple question"


def test_run_workflow_never_raises_when_no_api_key(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> object:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    monkeypatch.setattr(workflow, "get_client", _raise)
    result = workflow.run_workflow(AskRequest(question="q"))
    assert result.answer == ""
    assert "OPENAI_API_KEY" in result.notes


# --- run_workflow: successful multi-step execution -------------------------------


def _stub_two_step_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(
        workflow,
        "_plan_workflow",
        lambda *a, **k: {
            "steps": [
                {
                    "category": "coding",
                    "instruction": "write it",
                    "produces_artefact": False,
                    "artefact": "",
                    "inputs": [],
                },
                {
                    "category": "debugging",
                    "instruction": "check it",
                    "produces_artefact": False,
                    "artefact": "",
                    "inputs": [],
                },
            ],
            "synthesis_instruction": "combine both",
        },
    )


def test_run_workflow_executes_every_step_plus_synthesis(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        calls.append(
            {"question": req.question, "category": kwargs.get("forced_category")}
        )
        return AskResponse(
            answer=f"answer for {kwargs.get('forced_category')}",
            mode_used="auto->fast",
            notes="n",
            model="gpt-5-mini",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.001,
        )

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)

    result = workflow.run_workflow(AskRequest(question="do a two-part task"))

    assert len(calls) == 3  # 2 steps + synthesis
    assert calls[0]["category"] == "coding"
    assert calls[1]["category"] == "debugging"
    assert calls[2]["category"] == "summarization"
    assert result.mode_used == "workflow(3 steps)"
    assert result.answer == "answer for summarization"
    assert result.workflow_steps is not None
    assert len(result.workflow_steps) == 3
    assert all(s.status == "ok" for s in result.workflow_steps)
    assert result.workflow_steps[0].category == "coding"
    assert result.workflow_steps[0].model == "gpt-5-mini"
    assert result.input_tokens == 30  # 10 * 3 steps
    assert result.output_tokens == 60


def test_run_workflow_folds_prior_step_answers_into_later_steps(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)
    seen_questions: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        seen_questions.append(req.question)
        return AskResponse(answer="step output XYZ", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="do a two-part task"))

    # The second step's prompt must include the first step's own answer.
    assert "step output XYZ" in seen_questions[1]
    # The synthesis prompt must include both steps' answers.
    assert seen_questions[2].count("step output XYZ") == 2


def test_run_workflow_surfaces_partial_results_when_a_step_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        if kwargs.get("forced_category") == "debugging":
            return AskResponse(
                answer="", mode_used="auto->fast", notes="budget refused"
            )
        return AskResponse(
            answer=f"answer for {kwargs.get('forced_category')}",
            mode_used="auto->fast",
            notes="n",
        )

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    result = workflow.run_workflow(AskRequest(question="do a two-part task"))

    assert result.workflow_steps is not None
    failed = [s for s in result.workflow_steps if s.status == "failed"]
    assert len(failed) == 1
    assert failed[0].category == "debugging"
    # The workflow still completes (synthesis still runs on the surviving step).
    assert result.answer == "answer for summarization"
    assert "some steps failed" in result.notes


def test_run_workflow_falls_back_to_context_when_synthesis_itself_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        if kwargs.get("forced_category") == "summarization":
            return AskResponse(answer="", mode_used="auto->fast", notes="failed")
        return AskResponse(
            answer=f"answer for {kwargs.get('forced_category')}",
            mode_used="auto->fast",
            notes="n",
        )

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    result = workflow.run_workflow(AskRequest(question="do a two-part task"))

    # Never an empty final answer — surfaces the completed steps instead.
    assert result.answer.strip() != ""
    assert "answer for coding" in result.answer


# --- run_workflow: budget refusal ------------------------------------------------


def test_run_workflow_refuses_before_any_model_call_when_budget_reservation_fails(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)
    calls = []
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: (
            calls.append(1) or AskResponse(answer="x", mode_used="m", notes="n")
        ),
    )
    monkeypatch.setattr(
        budget, "reserve_workflow", lambda *a, **k: ("Daily budget reached.", None)
    )
    monkeypatch.setattr(workflow.budget, "reserve_workflow", budget.reserve_workflow)

    result = workflow.run_workflow(AskRequest(question="do a two-part task"))

    assert result.answer == ""
    assert "Daily budget reached" in result.notes
    assert calls == []  # no model call happened at all


def test_reserve_workflow_sizes_the_reservation_as_steps_times_per_step_cap(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "1000")
    # gpt-5 output $10/1M tokens; 3 calls * 100 tokens = 300 tokens -> $0.003
    note, reservation_id = budget.reserve_workflow("gpt-5", 100, 3, "", owner=None)
    assert note is None
    assert reservation_id is not None


def test_reserve_workflow_atomicity_matches_single_reserve_semantics(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent workflow reservations whose combined worst case exceeds
    the cap must not both be admitted — same guarantee test_budget.py proves
    for reserve() itself, since reserve_workflow is a thin wrapper around the
    same atomic try_reserve_spend() transaction."""
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.015")
    # gpt-5 output 10/1M; 1 step * 1000 tok -> $0.01 worst case each.
    note1, reservation1 = budget.reserve_workflow("gpt-5", 1000, 1, "", owner=None)
    assert note1 is None
    assert reservation1 is not None
    note2, reservation2 = budget.reserve_workflow("gpt-5", 1000, 1, "", owner=None)
    assert note2 is not None  # refused: already reserved + new > cap
    assert reservation2 is None


# --- stream_workflow ---------------------------------------------------------------


def test_stream_workflow_delegates_entirely_on_unparseable_plan(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: None)

    def fake_stream_orchestrator(req: AskRequest, **kwargs: object):
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}}
        yield {"event": "delta", "data": {"text": "hi"}}
        yield {
            "event": "done",
            "data": {"answer": "hi", "mode_used": "auto->fast", "notes": "n"},
        }

    monkeypatch.setattr(workflow, "stream_orchestrator", fake_stream_orchestrator)

    events = list(workflow.stream_workflow(AskRequest(question="q")))
    names = [e["event"] for e in events]
    assert names == ["meta", "delta", "done"]
    # No "step" events at all — a completely ordinary streamed answer.
    assert "step" not in names


def test_stream_workflow_emits_step_events_and_final_done(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        return AskResponse(
            answer=f"answer for {kwargs.get('forced_category')}",
            mode_used="auto->fast",
            notes="n",
            model="gpt-5-mini",
        )

    def fake_stream_orchestrator(req: AskRequest, **kwargs: object):
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "gpt-5"}}
        yield {"event": "delta", "data": {"text": "final "}}
        yield {"event": "delta", "data": {"text": "answer"}}
        yield {
            "event": "done",
            "data": {
                "answer": "final answer",
                "mode_used": "auto->fast",
                "notes": "n",
                "model": "gpt-5",
                "input_tokens": 5,
                "output_tokens": 5,
                "cost_usd": 0.0001,
            },
        }

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(workflow, "stream_orchestrator", fake_stream_orchestrator)

    events = list(workflow.stream_workflow(AskRequest(question="do a two-part task")))
    names = [e["event"] for e in events]

    assert names[0] == "meta"
    step_events = [e for e in events if e["event"] == "step"]
    # 2 steps * 2 events (running/ok) + synthesis * 2 events = 6
    assert len(step_events) == 6
    assert step_events[0]["data"]["status"] == "running"
    assert step_events[1]["data"]["status"] == "ok"
    assert step_events[-1]["data"]["category"] == "summarization"

    done_event = next(e for e in events if e["event"] == "done")
    assert done_event["data"]["answer"] == "final answer"
    assert len(done_event["data"]["workflow_steps"]) == 3


def test_stream_workflow_releases_reservation_on_generator_exit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)
    released: list[int | None] = []
    monkeypatch.setattr(workflow.budget, "release", lambda rid: released.append(rid))

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        return AskResponse(answer="ok", mode_used="auto->fast", notes="n")

    def fake_stream_orchestrator(req: AskRequest, **kwargs: object):
        yield {"event": "meta", "data": {}}
        yield {"event": "delta", "data": {"text": "partial"}}

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(workflow, "stream_orchestrator", fake_stream_orchestrator)

    gen = workflow.stream_workflow(AskRequest(question="do a two-part task"))
    next(gen)  # meta
    next(gen)  # step running
    next(gen)  # step ok
    next(gen)  # step running (2nd)
    next(gen)  # step ok (2nd)
    next(gen)  # step running (synthesis)
    gen.close()  # simulate client disconnect mid-synthesis-stream

    assert released  # the workflow-level placeholder was released


# --- HTTP integration: ask / ask-stream with mode="workflow" ---------------------


def test_ask_conversation_workflow_mode_persists_workflow_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages as messages_module

    def fake_run_workflow(req: AskRequest, owner: str | None = None) -> AskResponse:
        return AskResponse(
            answer="the final answer",
            mode_used="workflow(3 steps)",
            notes="Workflow: 2 step(s) + synthesis",
            workflow_steps=[
                WorkflowStep(
                    category="coding",
                    instruction="write it",
                    model="gpt-5-mini",
                    status="ok",
                ),
                WorkflowStep(
                    category="summarization",
                    instruction="combine",
                    model="gpt-5",
                    status="ok",
                ),
            ],
        )

    monkeypatch.setattr(messages_module, "run_workflow", fake_run_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "do a two-part task", "mode": "workflow"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode_used"] == "workflow(3 steps)"
    assert len(body["workflow_steps"]) == 2

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert len(assistant_message["workflow_steps"]) == 2
    assert assistant_message["workflow_steps"][0]["category"] == "coding"


def test_auto_routed_workflow_carries_its_breakdown_and_failure_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AUTO-ROUTED workflow returns through the ORDINARY ask path, not the
    mode="workflow" branch, because the decision is made inside the
    orchestrator -- so both workflow-specific fields have to be carried there
    too, and neither was.

    Caught by running the real thing three times rather than by any test: all
    three came back `auto->workflow(5 steps)` with an EMPTY breakdown and a NULL
    workflow_steps column, and a stopped step's plain-English message would
    have been dropped on the one path production actually uses (AUTO_WORKFLOW).
    The existing test above covers mode="workflow" and passed throughout.
    """
    import app.routers.messages as messages_module

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        # The shape run_orchestrator returns when it auto-routes into a
        # workflow that had to stop a step (see orchestrator._should_auto_
        # workflow -> workflow.run_workflow).
        return AskResponse(
            answer="the final answer",
            mode_used="auto->workflow(3 steps)",
            notes="Workflow: 3 step(s) (2 + synthesis) [step 2 needed x.csv]",
            failure_message="One step of this workflow was skipped.",
            workflow_steps=[
                WorkflowStep(
                    category="analysis",
                    instruction="summarise",
                    model="gpt-5",
                    status="ok",
                ),
                WorkflowStep(
                    category="analysis",
                    instruction="chart it",
                    model="",
                    status="failed",
                ),
            ],
        )

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run_orchestrator)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "summary, spreadsheet and chart", "mode": "auto"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode_used"] == "auto->workflow(3 steps)"

    # The breakdown reaches the client...
    assert len(body["workflow_steps"] or []) == 2, (
        "the per-step breakdown was dropped on the auto-routed path"
    )
    assert [s["status"] for s in body["workflow_steps"]] == ["ok", "failed"]
    # ...and so does the plain-English reason, rather than only the raw note.
    assert body["failure_message"] == "One step of this workflow was skipped."

    # ...and the breakdown survives persistence, so it is still there on reload.
    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert len(assistant_message["workflow_steps"] or []) == 2, (
        "the breakdown was not persisted for an auto-routed workflow"
    )
    assert assistant_message["workflow_steps"][1]["status"] == "failed"


def test_an_ordinary_answer_carries_no_workflow_breakdown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the fix: threading the two fields through the ordinary
    path must not invent them for an answer that never was a workflow."""
    import app.routers.messages as messages_module

    monkeypatch.setattr(
        messages_module,
        "run_orchestrator",
        lambda req, **kwargs: AskResponse(
            answer="a plain answer", mode_used="auto->fast", notes="n"
        ),
    )

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    body = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what is 2+2", "mode": "auto"},
    ).json()

    assert body["workflow_steps"] is None
    assert body["failure_message"] is None


def test_ask_conversation_non_workflow_mode_never_calls_run_workflow(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages as messages_module

    calls: list[int] = []
    monkeypatch.setattr(
        messages_module,
        "run_workflow",
        lambda *a, **k: (
            calls.append(1) or AskResponse(answer="x", mode_used="m", notes="n")
        ),
    )

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        return AskResponse(answer="ordinary answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run_orchestrator)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    res = client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert res.status_code == 200
    assert res.json()["answer"] == "ordinary answer"
    assert calls == []


def test_ask_conversation_stream_workflow_mode_emits_step_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages as messages_module

    def fake_stream_workflow(req: AskRequest, owner: str | None = None):
        yield {"event": "meta", "data": {"mode_used": "workflow(1 steps)", "model": ""}}
        yield {
            "event": "step",
            "data": {
                "index": 0,
                "total": 2,
                "category": "coding",
                "instruction": "x",
                "status": "running",
            },
        }
        yield {
            "event": "step",
            "data": {
                "index": 0,
                "total": 2,
                "category": "coding",
                "instruction": "x",
                "status": "ok",
            },
        }
        yield {"event": "delta", "data": {"text": "final answer"}}
        yield {
            "event": "done",
            "data": {
                "answer": "final answer",
                "mode_used": "workflow(1 steps)",
                "notes": "n",
                "workflow_steps": [
                    {
                        "category": "coding",
                        "instruction": "x",
                        "model": "gpt-5",
                        "status": "ok",
                    }
                ],
            },
        }

    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    with client.stream(
        "POST",
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "do a task", "mode": "workflow"},
    ) as res:
        body = "".join(res.iter_text())

    assert "event: step" in body
    assert "event: done" in body
    assert '"answer": "final answer"' in body

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert assistant_message["workflow_steps"][0]["category"] == "coding"


def test_ask_conversation_stream_workflow_mode_does_not_save_empty_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.routers.messages as messages_module

    def fake_stream_workflow(req: AskRequest, owner: str | None = None):
        yield {"event": "meta", "data": {"mode_used": "workflow(1 steps)", "model": ""}}
        yield {
            "event": "done",
            "data": {
                "answer": "",
                "mode_used": "workflow(1 steps)",
                "notes": "Daily budget reached.",
            },
        }

    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    with client.stream(
        "POST",
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "do a task", "mode": "workflow"},
    ) as res:
        list(res.iter_text())

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assert all(m["role"] != "assistant" for m in persisted)


# --- AUTO_WORKFLOW: the router-chosen half -------------------------------------


def test_auto_routed_workflow_is_tagged_so_the_mode_badge_shows_the_decision(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto->workflow(2 steps)` rather than `workflow(2 steps)`, so an
    automatic routing decision reads like every other one (`auto->fast`,
    `auto->clarify`) instead of being indistinguishable from a mode the user
    picked by hand."""
    _stub_two_step_plan(monkeypatch)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(answer="part", mode_used="m", notes="n"),
    )
    monkeypatch.setattr(workflow, "stream_orchestrator", lambda *a, **k: iter(()))

    auto = workflow.run_workflow(AskRequest(question="two artefacts"), auto_routed=True)
    manual = workflow.run_workflow(AskRequest(question="two artefacts"))

    assert auto.mode_used == "auto->workflow(3 steps)"
    assert manual.mode_used == "workflow(3 steps)"


def test_auto_routed_workflow_degrades_to_single_shot_when_budget_refuses(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user asked a question, not for a workflow — a plain answer they
    can afford beats a refusal they never invited. The manual path still
    refuses, because there the user DID ask for a workflow."""
    _stub_two_step_plan(monkeypatch)
    fallback_calls: list[dict[str, object]] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        fallback_calls.append(dict(kwargs))
        return AskResponse(answer="a plain answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(
        workflow.budget,
        "reserve_workflow",
        lambda *a, **k: ("Daily budget reached.", None),
    )

    result = workflow.run_workflow(
        AskRequest(question="two artefacts"),
        auto_routed=True,
        fallback_category="planning",
    )

    assert result.answer == "a plain answer"
    assert len(fallback_calls) == 1
    # Reuses the category the router already classified -> no SECOND
    # classifier call, and cannot bounce back into a workflow.
    assert fallback_calls[0]["forced_category"] == "planning"
    assert fallback_calls[0]["allow_auto_workflow"] is False


def test_manual_workflow_still_refuses_when_budget_refuses(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_two_step_plan(monkeypatch)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(answer="x", mode_used="m", notes="n"),
    )
    monkeypatch.setattr(
        workflow.budget,
        "reserve_workflow",
        lambda *a, **k: ("Daily budget reached.", None),
    )

    result = workflow.run_workflow(AskRequest(question="two artefacts"))

    assert result.answer == ""
    assert "Daily budget reached" in result.notes


def test_auto_routed_plan_failure_falls_back_without_reclassifying(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: None)
    seen: list[dict[str, object]] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.append(dict(kwargs))
        return AskResponse(answer="single shot", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)

    result = workflow.run_workflow(
        AskRequest(question="q"), auto_routed=True, fallback_category="coding"
    )

    assert result.answer == "single shot"
    assert seen[0]["forced_category"] == "coding"
    assert seen[0]["allow_auto_workflow"] is False


def test_a_failed_step_never_discards_the_steps_that_succeeded(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing four completed steps because the fifth failed is the worst
    outcome available. The completed work survives, the failure is stated in
    plain English, and every step's own status is preserved."""
    _stub_two_step_plan(monkeypatch)
    answers = iter(
        [
            AskResponse(answer="STEP ONE WORKED", mode_used="m", notes="n"),
            AskResponse(answer="", mode_used="m", notes="provider timed out"),
            AskResponse(answer="", mode_used="m", notes="synthesis also failed"),
        ]
    )
    monkeypatch.setattr(workflow, "run_orchestrator", lambda *a, **k: next(answers))

    result = workflow.run_workflow(AskRequest(question="two artefacts"))

    # The surviving step's real content is in the answer, not thrown away.
    assert "STEP ONE WORKED" in result.answer
    assert result.workflow_steps is not None
    statuses = [s.status for s in result.workflow_steps]
    assert "ok" in statuses and "failed" in statuses
    # ...and the user is told, rather than silently handed a partial answer.
    assert "some steps failed" in result.notes


# --- deliverable-faithful decomposition + artefact-capable steps --------------


_XLSX_MIME_FOR_TEST = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

_ARTEFACT_PLAN: dict[str, object] = {
    "steps": [
        {
            "category": "analysis",
            "instruction": "Write a short summary of Q3 revenue.",
            "produces_artefact": False,
            "artefact": "",
            "inputs": [],
        },
        {
            "category": "summarization",
            "instruction": "Produce an .xlsx file listing Q3 revenue by region.",
            "produces_artefact": True,
            "artefact": "an .xlsx of Q3 revenue by region",
            "inputs": [],
        },
    ],
    "synthesis_instruction": "combine",
}


def _stub_artefact_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: _ARTEFACT_PLAN)


def _file_result(filename: str) -> AskResponse:
    """An AskResponse shaped like a step that really produced a file."""
    return AskResponse(
        answer=f"Wrote {filename}.",
        mode_used="auto->smart",
        notes="n",
        code_results=[
            {
                "code": "df.to_excel(...)",
                "logs": "ok",
                "images": [],
                "files": [
                    {
                        "filename": filename,
                        "mime_type": _XLSX_MIME_FOR_TEST,
                        "data": f"data:{_XLSX_MIME_FOR_TEST};base64,ZmFrZQ==",
                    }
                ],
            }
        ],
    )


def test_plan_parsing_maps_an_artefact_to_a_producing_step() -> None:
    raw = (
        '{"steps": ['
        '{"category": "analysis", "instruction": "summarise", '
        '"produces_artefact": false, "artefact": ""},'
        '{"category": "coding", "instruction": "produce the .xlsx", '
        '"produces_artefact": true, "artefact": "an .xlsx of revenue"}'
        '], "synthesis_instruction": "combine"}'
    )
    plan = workflow._parse_plan_json(raw, cap=4)
    assert plan is not None
    assert [s["produces_artefact"] for s in plan["steps"]] == [False, True]
    assert plan["steps"][1]["artefact"] == "an .xlsx of revenue"


def test_an_artefact_step_demands_a_real_file_in_its_prompt() -> None:
    prompt = workflow._step_prompt(
        "the request", "produce the sheet", 0, 2, [], artefact="an .xlsx of revenue"
    )
    assert "PRODUCE A REAL FILE" in prompt
    assert "an .xlsx of revenue" in prompt
    # The exact failure being designed out.
    assert "markdown" in prompt


def test_a_prose_step_prompt_is_unchanged_by_the_artefact_wording() -> None:
    prompt = workflow._step_prompt("the request", "summarise it", 0, 2, [])
    assert "PRODUCE A REAL FILE" not in prompt


def test_an_artefact_step_runs_with_code_execution_required(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2: the artefact step asks for code execution; the prose step does
    NOT, so its cheap category routing is untouched."""
    _stub_artefact_plan(monkeypatch)
    seen: list[tuple[str, object]] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.append(
            (str(kwargs.get("forced_category")), kwargs.get("require_code_execution"))
        )
        return AskResponse(answer="x", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    assert ("analysis", False) in seen  # prose step: routing untouched
    assert ("summarization", True) in seen  # artefact step: code exec required


def test_step_files_survive_onto_the_final_message(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a file a STEP produced must reach the final answer,
    not be dropped. Previously code_results appeared nowhere in workflow.py."""
    _stub_artefact_plan(monkeypatch)
    answers = iter(
        [
            AskResponse(answer="prose summary", mode_used="m", notes="n"),
            _file_result("q3_revenue.xlsx"),
            AskResponse(answer="final answer", mode_used="m", notes="n"),
        ]
    )
    monkeypatch.setattr(workflow, "run_orchestrator", lambda *a, **k: next(answers))

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    assert result.code_results, "step artefacts were dropped"
    files = [f for cr in result.code_results for f in (cr.files or [])]
    assert [f.filename for f in files] == ["q3_revenue.xlsx"]


def test_synthesis_is_forbidden_from_re_rendering_a_real_file(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_artefact_plan(monkeypatch)
    prompts: list[str] = []
    answers = iter(
        [
            AskResponse(answer="prose", mode_used="m", notes="n"),
            _file_result("q3_revenue.xlsx"),
            AskResponse(answer="final", mode_used="m", notes="n"),
        ]
    )

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        return next(answers)

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    synthesis_prompt = prompts[-1]
    assert "ALREADY ATTACHED" in synthesis_prompt
    assert "q3_revenue.xlsx" in synthesis_prompt
    assert "Do NOT reproduce their contents" in synthesis_prompt


def test_synthesis_may_use_a_markdown_table_when_no_file_was_produced(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degrade path (item 6): with CODE_EXECUTION off an artefact step
    returns prose, and a markdown table is then the CORRECT output — so the
    prohibition must not appear. This is the direction that never gets
    exercised on an install where the flag happens to be on."""
    _stub_artefact_plan(monkeypatch)
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        # No code_results anywhere: exactly what an artefact step degrades to.
        return AskResponse(answer="a markdown table", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    synthesis_prompt = prompts[-1]
    assert "ALREADY ATTACHED" not in synthesis_prompt
    assert "Do NOT reproduce their contents" not in synthesis_prompt
    # ...and the workflow still succeeds, never an error.
    assert result.answer
    assert result.code_results is None


def test_synthesis_is_told_not_to_link_a_file_it_cannot_link_to(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attached file has no address. It reaches the browser as a data: URI
    the app builds, so a markdown link the model writes for it is dead however
    it is spelled — which is exactly what a live run shipped ("Download
    Spreadsheet: items_14_onwards.xlsx", linked, going nowhere)."""
    _stub_artefact_plan(monkeypatch)
    prompts: list[str] = []
    answers = iter(
        [
            AskResponse(answer="prose", mode_used="m", notes="n"),
            _file_result("q3_revenue.xlsx"),
            AskResponse(answer="final", mode_used="m", notes="n"),
        ]
    )

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        return next(answers)

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    synthesis_prompt = prompts[-1]
    assert "PLAIN TEXT" in synthesis_prompt
    assert "markdown link" in synthesis_prompt
    assert "leads nowhere" in synthesis_prompt


def test_a_run_that_produced_no_file_is_told_to_say_so_not_offer_a_download(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug's other half. With CODE_EXECUTION off (the shipped default) an
    artefact step degrades to prose and NOTHING is attached — and until now
    nothing told the synthesis that, so it offered a download for a file that
    was never produced. The promise is named back at it explicitly."""
    _stub_artefact_plan(monkeypatch)
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        return AskResponse(answer="a markdown table", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    synthesis_prompt = prompts[-1]
    assert "NO FILE WAS PRODUCED" in synthesis_prompt
    # Named from the plan's own artefact wording, which carries no filename —
    # see _promised_artefacts on why the guard cannot key on filenames alone.
    assert "an .xlsx of Q3 revenue by region" in synthesis_prompt
    assert "do NOT offer a download" in synthesis_prompt


def test_a_plan_promising_nothing_gets_neither_download_paragraph(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both paragraphs are conditional. An ordinary prose workflow never
    promised a file, so telling it 'NO FILE WAS PRODUCED' would be noise
    about something the user never asked for."""
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(
        workflow,
        "_plan_workflow",
        lambda *a, **k: {
            "steps": [
                {
                    "category": "analysis",
                    "instruction": "Summarise Q3 revenue.",
                    "produces_artefact": False,
                    "artefact": "",
                    "inputs": [],
                }
            ],
            "synthesis_instruction": "combine",
        },
    )
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        return AskResponse(answer="prose", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    workflow.run_workflow(AskRequest(question="summarise Q3"))

    synthesis_prompt = prompts[-1]
    assert "NO FILE WAS PRODUCED" not in synthesis_prompt
    assert "ALREADY ATTACHED" not in synthesis_prompt


def test_a_promised_file_that_code_execution_could_not_build_says_so(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degradation used to leave no trace but a server-side log line, so a
    request for a spreadsheet came back as prose about a spreadsheet with
    nothing anywhere saying why. The headline names the flag and where to
    change it; the raw detail (filenames, cause) stays in `notes`."""
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: False)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(
            answer="a markdown table", mode_used="m", notes="n"
        ),
    )

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    assert result.failure_message is not None
    assert "code execution is turned off" in result.failure_message
    assert "Settings" in result.failure_message
    # Plain English: the same no-internal-vocabulary bar the missing-input
    # headline is held to.
    assert "artefact" not in result.failure_message.lower()
    # ...with the technical half in notes, where the promised name belongs.
    assert "no file produced for an .xlsx of Q3 revenue by region" in result.notes
    assert "code execution off" in result.notes


def test_a_promised_file_blames_the_model_map_when_nothing_can_run_code(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag being ON is not enough: code execution reaches OpenAI- and
    Anthropic-served models only, so an all-Gemini map produces nothing with
    the checkbox ticked. Sending that operator to Settings to enable a flag
    that is already enabled would be worse than silence."""
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: True)
    monkeypatch.setattr(workflow, "code_execution_capable_model", lambda current: None)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(
            answer="a markdown table", mode_used="m", notes="n"
        ),
    )

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    assert result.failure_message is not None
    assert "none of the configured model tiers can run code" in result.failure_message
    assert "turned off" not in result.failure_message
    assert "no code-capable model tier configured" in result.notes


def test_a_claude_smart_tier_rescues_an_artefact_step_off_a_gemini_lane(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration that actually fixes the reported failure, end to end
    through the real routing: smart tier on Claude, everything else on Gemini.

    Worth its own test because the artefact step does NOT route to the smart
    tier on its own — _ARTEFACT_PLAN tags it `summarization`, which resolves
    to the FAST tier, which here is Gemini and cannot run code. Only
    _apply_code_execution_override moves it, and only by scanning the smart
    tier. Every earlier test of that override used an OpenAI rescue model; a
    Claude one takes a different branch of provider_of (the `claude` prefix
    rather than the bare-name fallback), so it is not the same assertion.
    """
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    _stub_artefact_plan(monkeypatch)

    seen: list[object] = []
    answers = iter(
        [
            AskResponse(answer="prose", mode_used="m", notes="n"),
            _file_result("q3_revenue.xlsx"),
            AskResponse(answer="final", mode_used="m", notes="n"),
        ]
    )

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.append(kwargs.get("require_code_execution"))
        return next(answers)

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    # The artefact step (and only it) asked for code execution...
    assert seen == [False, True, None]
    # ...the Claude tier is what the reservation was priced against, so the
    # budget quote and the routing cannot disagree (see _worst_case_model).
    assert (
        workflow._worst_case_model(
            {}, workflow._parse_plan_json(json.dumps(_ARTEFACT_PLAN), cap=4)["steps"]
        )
        == "claude-sonnet-5"
    )
    # ...and the file arrived, so nothing complains about configuration.
    assert result.failure_message is None
    files = [f for cr in result.code_results or [] for f in (cr.files or [])]
    assert [f.filename for f in files] == ["q3_revenue.xlsx"]


def test_a_claude_smart_tier_reaches_the_provider_with_code_execution_on(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same configuration, but WITHOUT stubbing run_orchestrator — the
    whole chain runs for real down to the provider boundary, which is the
    only place the two halves above could still disagree.

    Stubs `_call_model` and nothing above it, so the assertions are on what
    the real routing decided to hand a provider: which model, and whether
    code execution was on for it. A live Claude call is the one step this
    cannot cover (it needs a real ANTHROPIC_API_KEY); everything up to the
    request being built is exercised here.
    """
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    calls: list[tuple[object, object]] = []

    def fake_call_model(**kwargs: object) -> str:
        calls.append((kwargs.get("model"), kwargs.get("code_execution")))
        # Only the step actually ASKED for a file writes one, keyed off the
        # same "PRODUCE A REAL FILE" marker workflow.py emits and a real model
        # would act on (the E2E stub uses this marker for the same reason).
        # Having the tool available is not the same as using it — the prose
        # step gets it too and correctly produces nothing.
        wants_file = "PRODUCE A REAL FILE" in str(kwargs.get("question") or "")
        if kwargs.get("code_execution") and wants_file:
            kwargs["code_results"].append(  # type: ignore[union-attr]
                {
                    "code": "df.to_excel('q3_revenue.xlsx')",
                    "logs": "ok",
                    "images": [],
                    "files": [
                        {
                            "filename": "q3_revenue.xlsx",
                            "mime_type": _XLSX_MIME_FOR_TEST,
                            "data": f"data:{_XLSX_MIME_FOR_TEST};base64,ZmFrZQ==",
                        }
                    ],
                }
            )
        return "step answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    # The real shape of this configuration, asserted exactly — because it is
    # not the shape it looks like from the outside:
    #
    #   step 1  prose, category `analysis`      -> SMART tier, so Claude
    #   step 2  artefact, category `summarization` -> fast tier (Gemini),
    #           then moved to Claude by _apply_code_execution_override
    #   synthesis                                -> its own cheap lane, Gemini
    #
    # Note both Claude calls get code execution, not just the artefact one:
    # orchestrator's `code_execution_wanted` gates on the PROVIDER, not on
    # whether the step asked for a file, so pointing a tier at Claude offers
    # the tool to every call that lands there and the model decides for
    # itself. That is existing, documented behaviour — pinned here because it
    # is a live cost consequence of this exact config (CODE_EXECUTION_COST_USD
    # is charged per call that runs code), and a future per-step gate would
    # otherwise change it silently.
    assert calls == [
        ("claude-sonnet-5", True),
        ("claude-sonnet-5", True),
        ("gemini/gemini-flash-latest", False),
    ]

    # The deliverable, through the real plumbing.
    files = [f for cr in result.code_results or [] for f in (cr.files or [])]
    assert [f.filename for f in files] == ["q3_revenue.xlsx"]
    assert result.failure_message is None


def test_a_spreadsheet_request_comes_back_as_a_real_xlsx_over_the_anthropic_path(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spreadsheet request, end to end, with ONLY the Anthropic SDK stubbed.

    This is the seam nothing covered. The extraction side is well tested
    (test_llm.py, with block shapes ground-truthed against real transcripts
    after a bug where "mocked tests passed while every real call silently
    extracted nothing"), and the routing side is tested above — but no test
    ran a workflow all the way down the ANTHROPIC provider path and back,
    which is the path a Claude smart tier actually uses. The E2E suite cannot
    reach it: its stub speaks the OpenAI wire format, and `_call_model`
    dispatches Claude to a different client entirely.

    Everything real except the network: the plan, the routing, the code-
    execution override, call_anthropic's own request assembly and beta
    namespace selection, the tool-result extraction, and the Files API
    download that turns a file_id into the attachment bytes.
    """
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-lite-latest")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    xlsx_bytes = b"PK\x03\x04 not really a workbook, but the bytes that travel"
    betas_seen: list[object] = []

    def fake_create(**kwargs: object) -> types.SimpleNamespace:
        betas_seen.append(kwargs.get("betas"))
        sent = json.dumps(kwargs.get("messages"), default=str)
        blocks: list[types.SimpleNamespace] = [
            types.SimpleNamespace(type="text", text="Wrote the workbook.")
        ]
        if "PRODUCE A REAL FILE" in sent:
            # The real API's shapes, per providers.py's BUG HISTORY note: the
            # response block is `bash_code_execution`, never bare
            # `code_execution`.
            blocks += [
                types.SimpleNamespace(
                    type="server_tool_use",
                    id="toolu_1",
                    name="bash_code_execution",
                    input={"command": "python build_sheet.py"},
                ),
                types.SimpleNamespace(
                    type="bash_code_execution_tool_result",
                    tool_use_id="toolu_1",
                    content=types.SimpleNamespace(
                        type="bash_code_execution_result",
                        stdout="wrote q3_revenue.xlsx\n",
                        stderr="",
                        content=[
                            types.SimpleNamespace(
                                type="bash_code_execution_output",
                                file_id="file_xlsx_1",
                            )
                        ],
                    ),
                ),
            ]
        return types.SimpleNamespace(content=blocks, usage=None, stop_reason="end_turn")

    fake_client = types.SimpleNamespace(
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(create=fake_create),
            files=types.SimpleNamespace(
                retrieve_metadata=lambda file_id, **_kw: types.SimpleNamespace(
                    mime_type=_XLSX_MIME_FOR_TEST, filename="q3_revenue.xlsx"
                ),
                download=lambda file_id, **_kw: types.SimpleNamespace(
                    read=lambda: xlsx_bytes
                ),
            ),
        ),
        messages=types.SimpleNamespace(create=fake_create),
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)
    # The synthesis lands on the Gemini fast lane, which has no hosted tools
    # and is not what this test is about. Patched on orchestrator_calls, NOT
    # on providers: the dispatch layer does `from .providers import
    # call_litellm`, so the name is already bound there and patching the
    # source module would let a REAL Gemini call escape (it did, and the
    # failover to gpt-5 took eleven seconds to give up).
    monkeypatch.setattr(
        orchestrator_calls, "call_litellm", lambda *a, **k: "Here is the workbook."
    )

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    # The deliverable: real bytes on the final message, not prose about them.
    files = [f for cr in result.code_results or [] for f in (cr.files or [])]
    assert [f.filename for f in files] == ["q3_revenue.xlsx"]
    assert files[0].mime_type == _XLSX_MIME_FOR_TEST
    assert base64.b64decode(files[0].data.split(";base64,", 1)[1]) == xlsx_bytes

    # It went out on the beta namespace with the code-execution opt-in — the
    # ordinary namespace carries no such tool, so this is what makes it run.
    assert any(betas_seen), "code execution never used client.beta.messages"

    # And nothing complains, because there is genuinely nothing to complain
    # about: the file exists.
    assert result.failure_message is None


def test_a_claude_smart_tier_is_what_an_artefact_step_gets_moved_onto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single hop the test above depends on, isolated: a Gemini-routed
    artefact step is moved onto the Claude smart tier, and its notes say so
    rather than the move being silent."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")

    assert (
        orchestrator.code_execution_capable_model("gemini/gemini-flash-latest")
        == "claude-sonnet-5"
    )
    # And with the flag off it stays put — enabling Claude is not enough on
    # its own, which is exactly what the failure_message now says out loud.
    monkeypatch.setenv("CODE_EXECUTION", "false")
    assert workflow._no_artefact_reason() == workflow._FLAG_OFF


def test_a_run_that_delivered_its_file_says_nothing_about_code_execution(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole message is conditional on there being nothing to download.
    A workflow that delivered is not a workflow with a problem."""
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: True)
    answers = iter(
        [
            AskResponse(answer="prose", mode_used="m", notes="n"),
            _file_result("q3_revenue.xlsx"),
            AskResponse(answer="final", mode_used="m", notes="n"),
        ]
    )
    monkeypatch.setattr(workflow, "run_orchestrator", lambda *a, **k: next(answers))

    result = workflow.run_workflow(AskRequest(question="summary and a spreadsheet"))

    assert result.failure_message is None
    assert "no file produced" not in result.notes


def test_a_prose_only_workflow_is_never_told_it_owed_a_file(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was promised, so nothing is missing — with code execution off,
    which is the shipped default, every ordinary workflow would otherwise
    carry this notice."""
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(
        workflow,
        "_plan_workflow",
        lambda *a, **k: {
            "steps": [
                {
                    "category": "analysis",
                    "instruction": "Summarise Q3 revenue.",
                    "produces_artefact": False,
                    "artefact": "",
                    "inputs": [],
                }
            ],
            "synthesis_instruction": "combine",
        },
    )
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: False)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(answer="prose", mode_used="m", notes="n"),
    )

    result = workflow.run_workflow(AskRequest(question="summarise Q3"))

    assert result.failure_message is None


def test_promised_artefacts_prefers_a_real_filename_over_the_description() -> None:
    raw = (
        '{"steps": ['
        '{"category": "analysis", "instruction": "summarise", '
        '"produces_artefact": false, "artefact": ""},'
        '{"category": "coding", "instruction": "build it", '
        '"produces_artefact": true, "artefact": "a spreadsheet named tier_costs.xlsx"}'
        '], "synthesis_instruction": "combine"}'
    )
    plan = workflow._parse_plan_json(raw, cap=4)
    assert plan is not None
    # The filename, not the sentence around it — and the prose step, which
    # promised nothing, contributes nothing.
    assert workflow._promised_artefacts(plan["steps"]) == ["tier_costs.xlsx"]


def test_worst_case_pricing_uses_the_model_an_artefact_step_will_really_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 5: when the smart tier cannot run code, an artefact step is moved
    to one that can — so the reservation must price THAT model, or it quotes
    a model the workflow never actually uses."""
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gemini/gemini-flash-latest")
    monkeypatch.setenv("CODE_EXECUTION", "true")
    overrides: dict[str, str] = {}

    steps = _ARTEFACT_PLAN["steps"]
    assert isinstance(steps, list)
    prose_only = [steps[0]]
    assert (
        workflow._worst_case_model(overrides, prose_only)
        == "gemini/gemini-flash-latest"
    )
    assert workflow._worst_case_model(overrides, steps) == "gpt-5"


def test_step_count_label_counts_synthesis_everywhere() -> None:
    """PART 2: one convention — every step in the breakdown, synthesis
    included. The badge used to say 4 while the disclosure listed 5."""
    assert workflow._mode_tag(3, False) == "workflow(3 steps)"
    assert workflow._mode_tag(3, True) == "auto->workflow(3 steps)"


# --- cross-step artefact passing ------------------------------------------------
#
# The regression suite for the failure this section exists to prevent. A live
# three-artefact run produced a spreadsheet holding Fast=$0.0008 / Smart=$0.004
# and a chart of Fast=0.0003 / Smart=0.0020, both attached to the same message.
# The chart step had been told to work "using the tiers and costs from
# tier_costs.csv", ran `find / -iname "tier_costs.csv"`, found nothing (each
# step gets its own sandbox), and rebuilt the file from its own recollection.
# Nothing reported a problem; both attachments looked right on their own.


# The user's request, verbatim -- the fixture is built from this and nothing
# else, so the plan below is the shape a real planner has to produce for it.
_TIER_REQUEST = (
    "Write a short summary of how this app routes requests across model "
    "tiers, build a spreadsheet listing each tier with its model and typical "
    "cost per request, and produce a chart of cost by tier."
)

# What step 2 really wrote, from the live run.
_TRUE_COSTS = {"Fast": 0.0008, "Smart": 0.004}
# What step 3 charted instead, having found no file and fallen back on
# recollection. The fake model below does exactly this when -- and only when --
# the CSV's contents are absent from its prompt, so this suite fails loudly if
# the carry-forward ever stops working.
_MISREMEMBERED_COSTS = {"Fast": 0.0003, "Smart": 0.0020}

_TIER_MODELS = {"Fast": "gpt-5-mini", "Smart": "gpt-5"}

_TIER_CSV = "tier_costs.csv"
_TIER_CHART = "cost_by_tier.png"

_TIER_PLAN: dict[str, object] = {
    "steps": [
        {
            "category": "summarization",
            "instruction": (
                "Write a short summary of how requests are routed across tiers."
            ),
            "produces_artefact": False,
            "artefact": "",
            "inputs": [],
        },
        {
            "category": "analysis",
            "instruction": (
                f"Produce {_TIER_CSV} listing each tier with its model and "
                "typical cost per request."
            ),
            "produces_artefact": True,
            "artefact": f"{_TIER_CSV}, a spreadsheet of tier/model/cost",
            "inputs": [],
        },
        {
            "category": "analysis",
            "instruction": (
                f"Produce {_TIER_CHART}, a bar chart of cost by tier, using "
                f"the tiers and costs from {_TIER_CSV}."
            ),
            "produces_artefact": True,
            "artefact": f"{_TIER_CHART}, a bar chart of cost by tier",
            "inputs": [_TIER_CSV],
        },
    ],
    "synthesis_instruction": "Combine the summary, the spreadsheet and the chart.",
}


def _tier_csv_bytes(costs: dict[str, float]) -> str:
    """The spreadsheet step's real output as a data URL. Exactly three columns
    on every row, note rows included -- there are none, which is the point of
    the authoring rule now in the artefact step's prompt."""
    rows = ["tier,model,cost_per_request_usd"]
    rows.extend(f"{tier},{_TIER_MODELS[tier]},{cost}" for tier, cost in costs.items())
    body = ("\n".join(rows) + "\n").encode()
    return "data:text/csv;base64," + base64.b64encode(body).decode("ascii")


def _costs_from_csv_text(text: str) -> dict[str, float]:
    """tier -> cost, read back out of a CSV the way any consumer of the
    attached file would read it. Strict about width: a ragged row (the note
    row that used to be appended under a 3-column header) raises here rather
    than being silently tolerated."""
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if row]
    header, *data = rows
    assert len(header) == 3, f"unexpected header width: {header}"
    costs: dict[str, float] = {}
    for row in data:
        assert len(row) == len(header), f"ragged row breaks strict parsing: {row}"
        costs[row[0]] = float(row[2])
    return costs


def _plotted_from_prompt(prompt: str) -> dict[str, float]:
    """The fake chart model. It plots the values it can actually see in its
    own prompt, and falls back on recollection when it cannot see any -- which
    is precisely what the real model did. No filesystem, no invention beyond
    the one documented fallback."""
    opening = f"--- begin {_TIER_CSV} ---"
    closing = f"--- end {_TIER_CSV} ---"
    start = prompt.find(opening)
    end = prompt.find(closing)
    if start == -1 or end == -1:
        return dict(_MISREMEMBERED_COSTS)
    return _costs_from_csv_text(prompt[start + len(opening) : end].strip())


def _tier_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Wire up the three-step fixture; returns the list of prompts seen."""
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: _TIER_PLAN)
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompt = req.question
        prompts.append(prompt)
        if f"PRODUCE A REAL FILE: {_TIER_CSV}" in prompt:
            return AskResponse(
                answer=f"Wrote {_TIER_CSV}.",
                mode_used="m",
                notes="n",
                code_results=[
                    {
                        "code": "pd.DataFrame(rows).to_csv('tier_costs.csv')",
                        "logs": "wrote tier_costs.csv",
                        "images": [],
                        "files": [
                            {
                                "filename": _TIER_CSV,
                                "mime_type": "text/csv",
                                "data": _tier_csv_bytes(_TRUE_COSTS),
                            }
                        ],
                    }
                ],
            )
        if f"PRODUCE A REAL FILE: {_TIER_CHART}" in prompt:
            plotted = _plotted_from_prompt(prompt)
            return AskResponse(
                answer=f"Wrote {_TIER_CHART}.",
                mode_used="m",
                notes="n",
                code_results=[
                    {
                        "code": "plt.bar(t, c); plt.savefig('cost_by_tier.png')",
                        # What the chart actually plots, in a form the test can
                        # read back -- the stand-in for inspecting the pixels.
                        "logs": "plotted=" + json.dumps(plotted),
                        "images": [],
                        "files": [],
                    }
                ],
                images=["data:image/png;base64,ZmFrZQ=="],
            )
        return AskResponse(answer="prose", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    return prompts


def _attached_csv_costs(result: AskResponse) -> dict[str, float]:
    files = [f for cr in result.code_results or [] for f in (cr.files or [])]
    csv_files = [f for f in files if f.filename == _TIER_CSV]
    assert csv_files, "the spreadsheet never reached the final message"
    raw = base64.b64decode(csv_files[0].data.split(";base64,", 1)[1])
    return _costs_from_csv_text(raw.decode("utf-8"))


def _plotted_costs(result: AskResponse) -> dict[str, float]:
    for entry in result.code_results or []:
        logs = entry.logs or ""
        if logs.startswith("plotted="):
            loaded = json.loads(logs[len("plotted=") :])
            return dict(loaded)
    raise AssertionError("the chart step produced nothing that records its values")


def test_the_chart_plots_the_same_values_the_attached_spreadsheet_holds(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE assertion. Two artefacts on one message, both derived from the same
    figures, must agree -- which they only do if step 3 was handed step 2's
    actual file instead of being left to remember it."""
    _tier_orchestrator(monkeypatch)

    result = workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    spreadsheet = _attached_csv_costs(result)
    chart = _plotted_costs(result)

    assert spreadsheet == _TRUE_COSTS
    assert chart == spreadsheet, (
        "the attached chart and the attached spreadsheet disagree: "
        f"chart={chart} spreadsheet={spreadsheet}"
    )
    # Belt and braces: the disagreement this guards against is specifically
    # the recollection fallback, so name it.
    assert chart != _MISREMEMBERED_COSTS
    # Nothing was skipped -- every step ran and the workflow is clean.
    assert result.failure_message is None
    assert [s.status for s in result.workflow_steps or []] == ["ok"] * 4


def test_a_later_step_is_handed_the_file_not_a_description_of_it(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism behind the assertion above: the file's real contents are
    in the chart step's prompt, with the sandbox isolation spelled out so the
    model has no reason to go looking for it on disk."""
    prompts = _tier_orchestrator(monkeypatch)

    workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    chart_prompt = next(
        p for p in prompts if f"PRODUCE A REAL FILE: {_TIER_CHART}" in p
    )
    assert f"--- begin {_TIER_CSV} ---" in chart_prompt
    assert "0.0008" in chart_prompt and "0.004" in chart_prompt
    assert "own fresh sandbox" in chart_prompt.lower()
    assert "do not search for them" in chart_prompt

    # ...and the spreadsheet step, which needs nothing, is untouched by any of
    # this -- no phantom input block, no extra tokens.
    csv_prompt = next(p for p in prompts if f"PRODUCE A REAL FILE: {_TIER_CSV}" in p)
    assert "--- begin " not in csv_prompt


def test_an_artefact_step_is_told_a_note_is_not_a_data_row(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ragged-row fix. A live run appended `Note,All listed costs are
    illustrative examples, not live billing data,` under a three-column
    header -- unquoted commas, so the row splits wide and strict CSV parsing
    breaks. The caveat belongs in the prose, not the table."""
    prompts = _tier_orchestrator(monkeypatch)
    workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    csv_prompt = next(p for p in prompts if f"PRODUCE A REAL FILE: {_TIER_CSV}" in p)
    assert "exactly one header row" in csv_prompt
    assert "same number of columns" in csv_prompt
    assert "caveat" in csv_prompt
    assert "never leave a comma unquoted" in csv_prompt


# --- a missing input fails loudly ----------------------------------------------


def _degraded_csv_step(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The same three-step plan, but the spreadsheet step degrades to prose
    (CODE_EXECUTION off, a provider hiccup, a model that ignored the
    instruction) -- so tier_costs.csv never exists for step 3 to read."""
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: _TIER_PLAN)
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompts.append(req.question)
        return AskResponse(answer="here is a markdown table", mode_used="m", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    return prompts


def test_a_step_whose_input_never_materialised_errors_rather_than_improvising(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 6. The chart step declared it reads tier_costs.csv; no such file
    exists. It must be stopped, not left to invent the figures -- an
    internally inconsistent answer that looks right is worse than an error."""
    prompts = _degraded_csv_step(monkeypatch)

    result = workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    # The step never reached a model at all: no chart prompt was ever built.
    assert not any(f"PRODUCE A REAL FILE: {_TIER_CHART}" in p for p in prompts)

    statuses = [s.status for s in result.workflow_steps or []]
    assert statuses == ["ok", "ok", "failed", "ok"]
    chart_step = (result.workflow_steps or [])[2]
    assert chart_step.model == ""
    assert chart_step.cost_usd is None


def test_a_missing_input_is_surfaced_in_plain_english_with_raw_detail_behind_it(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 8bfc2b8 split: a readable headline for the user, the technical
    diagnostic left in `notes` for the details disclosure and the logs."""
    _degraded_csv_step(monkeypatch)

    result = workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    assert result.failure_message is not None
    plain = result.failure_message
    assert "skipped" in plain
    assert "guess" in plain
    # Plain English means no internal vocabulary leaking into the headline.
    assert "artefact" not in plain.lower()
    assert _TIER_CSV not in plain

    # ...while the raw detail names the step, the file, and what did exist.
    assert _TIER_CSV in result.notes
    assert "step 3" in result.notes
    assert "never produced" in result.notes
    assert "some steps failed" in result.notes


def test_a_skipped_step_is_not_also_blamed_on_the_producing_step(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both explanations can be live at once, but only one of them can be the
    reason. With code execution ON and an able model, a run whose step was
    stopped for a missing input already knows where its file went — adding
    "the step returned text instead" would name the wrong cause."""
    _degraded_csv_step(monkeypatch)
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: True)
    monkeypatch.setattr(
        workflow, "code_execution_capable_model", lambda current: "gpt-5"
    )

    result = workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    assert result.failure_message is not None
    assert "skipped" in result.failure_message
    assert "returned text instead" not in result.failure_message


def test_a_missing_input_never_discards_the_steps_that_already_succeeded(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 3. The guard must cost the user only the step it stopped."""
    _degraded_csv_step(monkeypatch)

    result = workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    assert result.answer.strip()
    statuses = [s.status for s in result.workflow_steps or []]
    assert statuses.count("ok") == 3, "surviving steps were discarded"


def test_synthesis_is_told_not_to_stand_in_for_the_step_that_was_stopped(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the guard is pointless: the synthesis sees a request for a
    chart, no chart, and a table of numbers -- and helpfully draws one in
    ASCII, which is where the whole problem started."""
    prompts = _degraded_csv_step(monkeypatch)

    workflow.run_workflow(AskRequest(question=_TIER_REQUEST))

    synthesis_prompt = prompts[-1]
    assert "COULD NOT BE COMPLETED" in synthesis_prompt
    assert "Do NOT stand in for the missing work" in synthesis_prompt
    assert "ASCII chart" in synthesis_prompt


def test_streaming_reports_the_stopped_step_and_carries_the_plain_english_out(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming path has its own copy of the loop, so it gets its own
    proof: a "failed" step event for the stopped step, and failure_message on
    the done event (which the client already reads as its headline)."""
    _degraded_csv_step(monkeypatch)
    monkeypatch.setattr(
        workflow,
        "stream_orchestrator",
        lambda *a, **k: iter(
            [
                {"event": "delta", "data": {"text": "final"}},
                {"event": "done", "data": {"model": "gpt-5"}},
            ]
        ),
    )

    events = list(workflow.stream_workflow(AskRequest(question=_TIER_REQUEST)))

    step_events = [e for e in events if e["event"] == "step"]
    chart_events = [e for e in step_events if e["data"]["index"] == 2]
    assert [e["data"]["status"] for e in chart_events] == ["running", "failed"]

    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["failure_message"]
    assert _TIER_CSV in done["data"]["notes"]


def test_streaming_also_says_why_no_file_could_be_produced(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The streaming path builds its own `done` payload, so the new headline
    gets its own proof there too rather than being assumed to have come along
    with the non-streaming one."""
    _stub_artefact_plan(monkeypatch)
    monkeypatch.setattr(workflow, "_code_execution_enabled", lambda: False)
    monkeypatch.setattr(
        workflow,
        "run_orchestrator",
        lambda *a, **k: AskResponse(
            answer="a markdown table", mode_used="m", notes="n"
        ),
    )
    monkeypatch.setattr(
        workflow,
        "stream_orchestrator",
        lambda *a, **k: iter(
            [
                {"event": "delta", "data": {"text": "final"}},
                {"event": "done", "data": {"model": "gpt-5"}},
            ]
        ),
    )

    events = list(
        workflow.stream_workflow(AskRequest(question="summary and a spreadsheet"))
    )

    done = next(e for e in events if e["event"] == "done")
    assert "code execution is turned off" in done["data"]["failure_message"]
    assert "code execution off" in done["data"]["notes"]


# --- resolution rules ------------------------------------------------------------


def test_a_producing_step_is_not_failed_for_naming_its_own_output() -> None:
    """The scan must never mistake an output for an input. Every artefact step
    names the file it is about to write; treating that as a required input
    would fail every one of them."""
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": f"Produce {_TIER_CSV} listing each tier.",
        "produces_artefact": True,
        "artefact": f"{_TIER_CSV}, a spreadsheet",
        "inputs": [],
    }
    resolved = workflow._resolve_step_inputs(step, {}, set())  # type: ignore[arg-type]
    assert resolved == workflow._StepInputs([], [], [])


def test_an_instruction_naming_a_real_earlier_file_gets_it_undeclared() -> None:
    """The safety net for a plan that describes the dependency but forgets to
    fill in `inputs`. Purely additive -- it can find a file, never invent a
    failure."""
    produced = {
        _TIER_CSV: {
            "filename": _TIER_CSV,
            "mime_type": "text/csv",
            "data": _tier_csv_bytes(_TRUE_COSTS),
        }
    }
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": f"Chart the costs from {_TIER_CSV}.",
        "produces_artefact": True,
        "artefact": f"{_TIER_CHART}, a bar chart",
        "inputs": [],
    }
    resolved = workflow._resolve_step_inputs(step, produced, set())  # type: ignore[arg-type]
    assert [name for name, _ in resolved.available] == [_TIER_CSV]
    assert "0.0008" in resolved.available[0][1]


def test_an_unrecognised_filename_in_an_instruction_is_never_a_failure() -> None:
    """The other half of "purely additive": a scanned name no earlier step
    owns is ignored, because a scan cannot tell "read x.csv" from "write
    x.csv". Only a planner-DECLARED input can fail a step."""
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Save the results to results.csv when you are done.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": [],
    }
    resolved = workflow._resolve_step_inputs(step, {}, set())  # type: ignore[arg-type]
    assert resolved.missing == []


def test_a_declared_input_resolves_across_a_changed_file_extension() -> None:
    """The planner names the file before it exists ("tier_costs.csv") and the
    producing model saves tier_costs.xlsx. The data is right there, so an
    unambiguous stem match counts rather than failing the step."""
    produced = {
        "tier_costs.xlsx": {
            "filename": "tier_costs.xlsx",
            "mime_type": "text/csv",
            "data": _tier_csv_bytes(_TRUE_COSTS),
        }
    }
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Chart it.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": [_TIER_CSV],
    }
    resolved = workflow._resolve_step_inputs(step, produced, set())  # type: ignore[arg-type]
    assert [name for name, _ in resolved.available] == ["tier_costs.xlsx"]
    assert resolved.missing == []


def test_an_input_of_a_format_this_module_cannot_carry_is_opaque_not_fatal() -> None:
    """A .docx has no text reader here, so no version of this code could hand
    its contents to a step. The line therefore sits at "could this input ever
    have been carried?", not "was it produced?".

    This case used to fail the step. It was moved because failing bought
    nothing -- the content is unavailable either way -- while costing every
    later step that needed THIS step's output: a real plan invented a
    plan_artifacts.pdf on a leading process step, and stopping the step that
    declared it took out the spreadsheet, the chart and the closing summary.
    The step now runs and is told in its prompt not to guess at the file.
    """
    produced = {
        "notes.docx": {
            "filename": "notes.docx",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "data": "data:application/octet-stream;base64,ZmFrZQ==",
        }
    }
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Summarise it.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": ["notes.docx"],
    }
    resolved = workflow._resolve_step_inputs(step, produced, set())  # type: ignore[arg-type]
    assert resolved.opaque == ["notes.docx"]
    assert resolved.missing == []
    assert resolved.unreadable == []
    assert resolved.available == []

    # ...and the step's prompt says so, rather than staying silent about it.
    prompt = workflow._step_prompt(
        "q", "Summarise it.", 1, 2, [], opaque_inputs=resolved.opaque
    )
    assert "notes.docx" in prompt
    assert "do NOT describe, quote, or reconstruct" in prompt


def test_a_carryable_input_that_cannot_be_decoded_still_fails_the_step() -> None:
    """The other side of that line, which must NOT have moved: a .xlsx is a
    format this module does read, so a corrupt one is a step that could have
    had its data and does not -- exactly the case where it would fill the gap
    from memory."""
    produced = {
        "tiers.xlsx": {
            "filename": "tiers.xlsx",
            "mime_type": _XLSX_MIME_FOR_TEST,
            "data": f"data:{_XLSX_MIME_FOR_TEST};base64,bm90LWEtd29ya2Jvb2s=",
        }
    }
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Chart it.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": ["tiers.xlsx"],
    }
    resolved = workflow._resolve_step_inputs(step, produced, set())  # type: ignore[arg-type]
    assert resolved.unreadable == ["tiers.xlsx"]
    assert resolved.available == []


def test_a_generated_xlsx_input_uses_the_same_path_an_upload_uses() -> None:
    """A generated workbook and an attached one reach a model in identical
    shape -- spreadsheet_ingestion.xlsx_to_text, already bounded per sheet."""
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["tier", "cost"])
    sheet.append(["Fast", 0.0008])
    buffer = BytesIO()
    book.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

    text = workflow._artefact_as_text(
        {
            "filename": "tiers.xlsx",
            "mime_type": _XLSX_MIME_FOR_TEST,
            "data": f"data:{_XLSX_MIME_FOR_TEST};base64,{encoded}",
        }
    )
    assert text is not None
    assert "0.0008" in text
    assert "Extracted from tiers.xlsx" in text


def test_carried_forward_text_is_bounded() -> None:
    """A step prompt cannot grow without limit however many inputs it
    declares -- AskRequest.question has its own hard cap, and the step's real
    instruction still has to fit."""
    big = ("x" * 40_000).encode()
    produced = {
        "big.txt": {
            "filename": "big.txt",
            "mime_type": "text/plain",
            "data": "data:text/plain;base64," + base64.b64encode(big).decode("ascii"),
        }
    }
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Use it.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": ["big.txt"],
    }
    resolved = workflow._resolve_step_inputs(step, produced, set())  # type: ignore[arg-type]
    text = resolved.available[0][1]
    assert len(text) < 40_000
    assert "truncated" in text


def test_the_plan_prompt_asks_for_inputs_and_named_artefacts() -> None:
    """The planner is where the dependency has to be declared; if the prompt
    does not ask for it, none of the above ever fires."""
    prompt = workflow._WORKFLOW_PLAN_PROMPT
    assert '"inputs"' in prompt
    assert "fresh sandbox" in prompt
    assert "FILENAME" in prompt


def test_the_plan_parser_reads_inputs_and_tolerates_their_absence() -> None:
    raw = (
        '{"steps": ['
        '{"category": "analysis", "instruction": "make the csv", '
        '"produces_artefact": true, "artefact": "tier_costs.csv", "inputs": []},'
        '{"category": "analysis", "instruction": "chart it", '
        '"produces_artefact": true, "artefact": "chart.png", '
        '"inputs": ["tier_costs.csv", "  "]}'
        '], "synthesis_instruction": "combine"}'
    )
    plan = workflow._parse_plan_json(raw, cap=4)
    assert plan is not None
    assert [s["inputs"] for s in plan["steps"]] == [[], ["tier_costs.csv"]]

    # A model that free-formed its plan and omitted `inputs` declares no
    # dependency, which is the safe reading -- an input this module cannot
    # satisfy fails its step, so inventing one would turn a sloppy plan into a
    # hard error.
    bare = (
        '{"steps": [{"category": "analysis", "instruction": "x"}], '
        '"synthesis_instruction": "y"}'
    )
    bare_plan = workflow._parse_plan_json(bare, cap=4)
    assert bare_plan is not None
    assert bare_plan["steps"][0]["inputs"] == []


# --- mode="workflow" on the retry paths ----------------------------------------
#
# RegenerateRequest.mode and AskRequest.mode both accept Mode.workflow, and both
# retry paths used to hand it straight to run_orchestrator — where decide_route
# has no Mode.workflow case, so it fell through to the FAST tier default. A
# caller who asked for a multi-step answer silently got a single-shot one at the
# tightest cap in the app, and nothing in the response said so. Honoured on all
# four halves now (regenerate/edit x streaming/not) or it is a silent downgrade
# on whichever half was missed.


def _workflow_answer() -> AskResponse:
    return AskResponse(
        answer="a workflow answer",
        mode_used="workflow(2 steps)",
        notes="Workflow: 2 step(s)",
        model="gpt-5",
        cost_usd=0.3,
        workflow_steps=[
            WorkflowStep(
                category="coding", instruction="build it", model="gpt-5", status="ok"
            )
        ],
    )


@pytest.mark.parametrize("path", ["regenerate", "edit"])
def test_a_workflow_retry_runs_a_workflow_not_a_fast_tier_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    import app.routers.messages as messages_module

    orchestrator_calls: list[AskRequest] = []
    workflow_calls: list[AskRequest] = []

    def fake_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        orchestrator_calls.append(req)
        return AskResponse(answer="single shot", mode_used="auto->fast", notes="n")

    def fake_workflow(req: AskRequest, **kwargs: object) -> AskResponse:
        workflow_calls.append(req)
        return _workflow_answer()

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_orchestrator)
    monkeypatch.setattr(messages_module, "run_workflow", fake_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    orchestrator_calls.clear()

    if path == "regenerate":
        body = client.post(
            f"/v1/conversations/{cid}/regenerate", json={"mode": "workflow"}
        ).json()
    else:
        messages = client.get(f"/v1/conversations/{cid}/messages").json()
        user_id = next(m for m in messages if m["role"] == "user")["id"]
        body = client.post(
            f"/v1/conversations/{cid}/messages/{user_id}/edit",
            json={"question": "q", "mode": "workflow"},
        ).json()

    assert len(workflow_calls) == 1, "the workflow was not run"
    assert orchestrator_calls == [], "the retry was silently downgraded to single-shot"
    assert body["mode_used"] == "workflow(2 steps)"
    assert body["workflow_steps"][0]["category"] == "coding"

    row = next(
        m
        for m in client.get(f"/v1/conversations/{cid}/messages").json()
        if m["role"] == "assistant"
    )
    assert row["workflow_steps"][0]["category"] == "coding"


@pytest.mark.parametrize("path", ["regenerate", "edit"])
def test_a_streaming_workflow_retry_replaces_the_answer_and_is_attributed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The streaming halves, which route through _stream_workflow_and_persist —
    it had no replace support at all, so honouring the mode there also meant
    teaching it to swap the old answer out and to record the retry, since a
    workflow retry would otherwise be the one retry retry_log cannot see."""
    import app.routers.messages as messages_module
    from app import database

    def fake_stream_workflow(req: AskRequest, owner: str | None = None):
        yield {"event": "meta", "data": {"mode_used": "workflow(1 steps)", "model": ""}}
        yield {
            "event": "done",
            "data": {
                "answer": "the workflow answer",
                "mode_used": "workflow(1 steps)",
                "notes": "n",
                "model": "gpt-5",
                "cost_usd": 0.25,
                "workflow_steps": [
                    {
                        "category": "coding",
                        "instruction": "x",
                        "model": "gpt-5",
                        "status": "ok",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        messages_module,
        "run_orchestrator",
        lambda req, **k: AskResponse(
            answer="the original answer",
            mode_used="auto->fast:coding",
            notes="n",
            model="gpt-5-mini",
            cost_usd=0.01,
        ),
    )
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream_workflow)

    if path == "regenerate":
        url = f"/v1/conversations/{cid}/regenerate/stream"
        payload: dict[str, object] = {"mode": "workflow"}
    else:
        messages = client.get(f"/v1/conversations/{cid}/messages").json()
        user_id = next(m for m in messages if m["role"] == "user")["id"]
        url = f"/v1/conversations/{cid}/messages/{user_id}/edit/stream"
        payload = {"question": "q", "mode": "workflow"}

    with client.stream("POST", url, json=payload) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())
    assert "event: done" in body

    rows = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_rows = [m for m in rows if m["role"] == "assistant"]
    # Replaced, not appended: one answer, and it is the workflow's.
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["content"] == "the workflow answer"
    assert assistant_rows[0]["workflow_steps"][0]["category"] == "coding"

    # ...and the retry is in the ledger, attributed to the ORIGINAL decision.
    attempts = database.retry_log_turn_rows(None, days=1)
    assert [a["attempt_index"] for a in attempts] == [1, 2]
    assert attempts[0]["tier"] == "fast"
    assert attempts[0]["cost_usd"] == pytest.approx(0.01)
    assert attempts[1]["cost_usd"] == pytest.approx(0.25)
    expected_signal = "edited" if path == "edit" else "regenerated_unrated"
    assert attempts[1]["signal"] == expected_signal


# --- a pin must not veto the SHAPE of the answer -------------------------------
#
# The tests above run on an UNPINNED conversation, which is why they passed while
# the pinned case was broken. `_pinned_ask_request` rewrote Mode.workflow to the
# pin's own tier, and regenerate/edit decide whether to run a workflow by reading
# the request it returns — so on a pinned conversation the decision was made after
# the evidence for it had been erased. `$ Retry as workflow` on a truncated answer
# dispatched an ordinary answer at the smart tier's 4000-token cap: the very
# ceiling that had just cut the answer off. The remedy was inert exactly where the
# UI offered it.


@pytest.mark.parametrize("path", ["regenerate", "edit"])
@pytest.mark.parametrize(
    "pin,expected_model",
    [
        ("claude-sonnet-5", "claude-sonnet-5"),  # a MODEL pin rides along
        ("smart", None),  # a TIER pin has nothing to force
    ],
    ids=["model_pin", "tier_pin"],
)
def test_a_workflow_retry_still_runs_a_workflow_on_a_pinned_conversation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    pin: str,
    expected_model: str | None,
) -> None:
    import app.routers.messages as messages_module

    orchestrator_calls: list[AskRequest] = []
    workflow_calls: list[AskRequest] = []

    def fake_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        orchestrator_calls.append(req)
        return AskResponse(answer="single shot", mode_used="auto->smart", notes="n")

    def fake_workflow(req: AskRequest, **kwargs: object) -> AskResponse:
        workflow_calls.append(req)
        return _workflow_answer()

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_orchestrator)
    monkeypatch.setattr(messages_module, "run_workflow", fake_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    client.put(f"/v1/conversations/{cid}/pin", json={"model": pin})
    orchestrator_calls.clear()

    if path == "regenerate":
        body = client.post(
            f"/v1/conversations/{cid}/regenerate", json={"mode": "workflow"}
        ).json()
    else:
        messages = client.get(f"/v1/conversations/{cid}/messages").json()
        user_id = next(m for m in messages if m["role"] == "user")["id"]
        body = client.post(
            f"/v1/conversations/{cid}/messages/{user_id}/edit",
            json={"question": "q", "mode": "workflow"},
        ).json()

    assert len(workflow_calls) == 1, "the pin swallowed mode=workflow"
    assert orchestrator_calls == [], "the retry was silently downgraded to single-shot"
    assert body["mode_used"] == "workflow(2 steps)"
    # A model pin is still honoured — every step runs on the pinned model.
    assert workflow_calls[0].model == expected_model


@pytest.mark.parametrize("path", ["regenerate", "edit"])
def test_a_streaming_workflow_retry_survives_a_pin_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Both halves or neither — the streaming twin is the one the UI actually
    calls (`$ Retry as workflow` posts to /regenerate/stream)."""
    import app.routers.messages as messages_module

    workflow_streams: list[AskRequest] = []

    def fake_stream_workflow(req: AskRequest, owner: str | None = None):
        workflow_streams.append(req)
        yield {"event": "meta", "data": {"mode_used": "workflow(1 steps)", "model": ""}}
        yield {
            "event": "done",
            "data": {
                "answer": "the workflow answer",
                "mode_used": "workflow(1 steps)",
                "notes": "n",
                "model": "gpt-5",
            },
        }

    def fail_if_streamed(*_a: object, **_k: object):
        raise AssertionError("the retry was downgraded to an ordinary stream")

    monkeypatch.setattr(
        messages_module,
        "run_orchestrator",
        lambda req, **k: AskResponse(
            answer="the original answer", mode_used="auto->fast", notes="n"
        ),
    )
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    client.put(f"/v1/conversations/{cid}/pin", json={"model": "claude-sonnet-5"})
    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream_workflow)
    monkeypatch.setattr(messages_module, "stream_orchestrator", fail_if_streamed)

    if path == "regenerate":
        url = f"/v1/conversations/{cid}/regenerate/stream"
        payload: dict[str, object] = {"mode": "workflow"}
    else:
        messages = client.get(f"/v1/conversations/{cid}/messages").json()
        user_id = next(m for m in messages if m["role"] == "user")["id"]
        url = f"/v1/conversations/{cid}/messages/{user_id}/edit/stream"
        payload = {"question": "q", "mode": "workflow"}

    with client.stream("POST", url, json=payload) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    assert "event: done" in body
    assert len(workflow_streams) == 1, "the pin swallowed mode=workflow"
    assert workflow_streams[0].model == "claude-sonnet-5"


@pytest.mark.parametrize(
    "pin,expected_mode,expected_model",
    [
        ("claude-sonnet-5", "smart", "claude-sonnet-5"),
        ("smart", "smart", None),
        ("fast", "fast", None),
    ],
)
def test_a_pin_still_overrides_every_other_mode(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    pin: str,
    expected_mode: str,
    expected_model: str | None,
) -> None:
    """The counterweight: workflow is the ONE exception, not the start of
    'pins are advisory'. An ordinary request under a pin is routed by the pin
    exactly as before — otherwise the fix above would have quietly turned a
    pinned conversation into an unpinned one."""
    import app.routers.messages as messages_module

    seen: list[AskRequest] = []

    def fake_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.append(req)
        return AskResponse(answer="an answer", mode_used="auto->smart", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_orchestrator)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "q"})
    client.put(f"/v1/conversations/{cid}/pin", json={"model": pin})
    seen.clear()

    client.post(f"/v1/conversations/{cid}/regenerate", json={"mode": "fast"})

    assert seen[0].mode.value == expected_mode
    assert seen[0].model == expected_model


# --- /v1/ask honours the pin for a workflow too ---------------------------------
#
# The last inconsistency in what a pin means. Every answering path applies the
# conversation's pin, and the retry paths apply it to a workflow — but /v1/ask's
# workflow branch passed the raw request straight to run_workflow, so a pinned
# conversation asked in workflow mode ran every step on the router's own choice
# of model. The pin meant different things depending on which button produced the
# workflow.


@pytest.mark.parametrize(
    "pin,expected_model",
    [
        ("claude-sonnet-5", "claude-sonnet-5"),  # a MODEL pin is forced
        ("smart", None),  # a TIER pin has nothing to force
        ("", None),  # no pin, nothing invented
    ],
    ids=["model_pin", "tier_pin", "no_pin"],
)
def test_ask_workflow_mode_honours_a_model_pin(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    pin: str,
    expected_model: str | None,
) -> None:
    import app.routers.messages as messages_module

    seen: list[AskRequest] = []

    def fake_workflow(req: AskRequest, **kwargs: object) -> AskResponse:
        seen.append(req)
        return _workflow_answer()

    monkeypatch.setattr(messages_module, "run_workflow", fake_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    if pin:
        client.put(f"/v1/conversations/{cid}/pin", json={"model": pin})

    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "a summary and a spreadsheet", "mode": "workflow"},
    )

    assert res.status_code == 200
    assert len(seen) == 1
    assert seen[0].model == expected_model
    assert seen[0].mode == Mode.workflow, "the pin must not undo the mode"


def test_ask_workflow_mode_still_operates_on_the_raw_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant applying the pin must not break. Workflow mode's whole
    premise is the raw new turn — no history, memory or library threading (see
    app/workflow.py's module docstring) — so the pin is applied to req.question,
    never to an assembled context prompt. Getting this wrong would smuggle the
    conversation's history into the one mode that deliberately excludes it."""
    import app.routers.messages as messages_module

    seen: list[AskRequest] = []
    monkeypatch.setattr(
        messages_module,
        "run_workflow",
        lambda req, **k: seen.append(req) or _workflow_answer(),
    )
    monkeypatch.setattr(
        messages_module,
        "run_orchestrator",
        lambda req, **k: AskResponse(
            answer="an earlier answer", mode_used="auto->fast", notes="n"
        ),
    )

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "an earlier question"}
    )
    client.put(f"/v1/conversations/{cid}/pin", json={"model": "claude-sonnet-5"})

    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "a summary and a spreadsheet", "mode": "workflow"},
    )

    assert seen[0].question == "a summary and a spreadsheet"
    assert "an earlier question" not in seen[0].question
    assert "an earlier answer" not in seen[0].question


def test_applying_a_pin_to_a_workflow_request_changes_nothing_else() -> None:
    """The pin is applied by rebuilding the request, and /v1/ask hands the result
    straight to run_workflow — so every OTHER field has to survive, including
    ones nothing reads today and ones added later. Asserted field-by-field off
    model_fields rather than by naming a few, because the failure mode being
    guarded is precisely a field nobody remembered to name: a dropped field reads
    as "this path has none", indistinguishable from absent. This is why
    _pinned_ask_request's workflow branch is a copy-with-overrides and not a
    field list (the same reasoning as _api_response's model_copy)."""
    from app.ask_support import _pinned_ask_request
    from app.schemas import AudioAttachment, FileAttachment

    req = AskRequest(
        question="a summary and a spreadsheet",
        mode=Mode.workflow,
        no_cache=True,
        model="gpt-5-mini",
        images=["data:image/png;base64,ZmFrZQ=="],
        files=[
            FileAttachment(
                filename="meeting.txt",
                mime_type="text/plain",
                data="data:text/plain;base64,aGVsbG8=",
            )
        ],
        audio=[
            AudioAttachment(
                filename="call.mp3",
                mime_type="audio/mpeg",
                data="data:audio/mpeg;base64,aGVsbG8=",
            )
        ],
        research=True,
        request_id="req-123",
    )

    out = _pinned_ask_request({"pinned_model": "claude-sonnet-5"}, req.question, req)

    # The two the pin is allowed to touch.
    assert out.model == "claude-sonnet-5"
    assert out.mode == Mode.workflow
    # Everything else, whatever the model happens to declare.
    for name in type(req).model_fields:
        if name in {"model"}:
            continue
        assert getattr(out, name) == getattr(req, name), f"{name} was dropped"


def test_streaming_ask_workflow_mode_honours_a_model_pin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves or neither."""
    import app.routers.messages as messages_module

    seen: list[AskRequest] = []

    def fake_stream_workflow(req: AskRequest, owner: str | None = None):
        seen.append(req)
        yield {"event": "meta", "data": {"mode_used": "workflow(1 steps)", "model": ""}}
        yield {
            "event": "done",
            "data": {
                "answer": "the workflow answer",
                "mode_used": "workflow(1 steps)",
                "notes": "n",
                "model": "claude-sonnet-5",
            },
        }

    monkeypatch.setattr(messages_module, "stream_workflow", fake_stream_workflow)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.put(f"/v1/conversations/{cid}/pin", json={"model": "claude-sonnet-5"})

    with client.stream(
        "POST",
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "a summary and a spreadsheet", "mode": "workflow"},
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    assert "event: done" in body
    assert len(seen) == 1
    assert seen[0].model == "claude-sonnet-5"
    assert seen[0].mode == Mode.workflow
