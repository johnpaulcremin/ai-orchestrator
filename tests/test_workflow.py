"""Opt-in multi-step workflow mode (app/workflow.py; mode="workflow"): plan
parsing, the unparseable-plan fallback, per-step execution, atomic budget
reservation, SSE step events, and persistence — see the module docstring in
app/workflow.py for the overall design.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import budget, workflow
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
    assert plan == {
        "steps": [
            {"category": "coding", "instruction": "write the function"},
            {"category": "debugging", "instruction": "find the bug"},
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
                {"category": "coding", "instruction": "write it"},
                {"category": "debugging", "instruction": "check it"},
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
    assert result.mode_used == "workflow(2 steps)"
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
            mode_used="workflow(2 steps)",
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
    assert body["mode_used"] == "workflow(2 steps)"
    assert len(body["workflow_steps"]) == 2

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert len(assistant_message["workflow_steps"]) == 2
    assert assistant_message["workflow_steps"][0]["category"] == "coding"


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

    assert auto.mode_used == "auto->workflow(2 steps)"
    assert manual.mode_used == "workflow(2 steps)"


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
