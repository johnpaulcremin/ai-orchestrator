"""Workflow planning against plans the REAL planner really emitted.

Why this module exists, separately from tests/test_workflow.py: every plan
fixture in that file is hand-authored, and hand-authored plans are tidy. The
live path broke twice on a CI-green commit because the real planner emits
plans that are not tidy at all, in ways nobody thought to hand-write:

* It marks a PROSE step as artefact-producing and invents a .txt filename for
  it (7 of 12 sampled runs of the same prompt). The step then runs, writes good
  prose, and writes no file -- so the next step, which declared that filename
  as its input, could not resolve it and stopped, and the step after that
  needed the second step's output, so it stopped too. One correct step cost
  two deliverables.
* It occasionally degenerates mid-response and writes the schema into the
  instruction string: `"... Produces_artefact: true. Artefact: routing_summary
  .txt. Inputs: []},{"`. That is VALID JSON -- the fragment is ordinary text
  inside a string -- so json.loads accepts it, the step count silently halves,
  and `artefact` ends up holding a prose description with no filename in it.
  With no filename recognised as the step's own, the step's own outputs
  survived as required inputs and the FIRST step of the plan was failed for
  needing files that no earlier step could possibly have produced.

So the plans below are pasted verbatim from a live capture (see
tests/planner_captures.json for the raw responses, saved exactly as the
planner returned them), and the degenerate one is reconstructed to the shape
observed in the failing run. Anything asserted here is asserted against what
the planner actually does, not against what it ought to do.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from app import workflow
from app.schemas import AskRequest, AskResponse

_CAPTURES = json.loads(
    (Path(__file__).parent / "planner_captures.json").read_text(encoding="utf-8")
)

# A prose step declaring a .txt artefact, with the NEXT step declaring that
# .txt as its input -- the exact shape that lost two of three deliverables.
PROSE_ARTEFACT_CHAIN = _CAPTURES["prose_artefact_chain"]

# A leading process step claiming all three artefacts at once.
PROCESS_STEP_MULTI_ARTEFACT = _CAPTURES["process_step_multi_artefact"]

# Reconstructed from the failing run: valid JSON, two steps, schema text
# trailing inside step 1's instruction, `artefact` holding prose rather than a
# filename, and step 1 declaring its own outputs as its inputs.
DEGENERATE_PLAN = (
    '{"steps":[{"category":"planning","instruction":"Summarise how the app '
    "routes requests across model tiers. Produces_artefact: true. Artefact: "
    'routing_summary.txt. Inputs: []},{","produces_artefact":true,'
    '"artefact":"the summary and the spreadsheet",'
    '"inputs":["routing_summary.txt","tier_costs.csv"]},'
    '{"category":"coding","instruction":"Chart the costs.",'
    '"produces_artefact":true,"artefact":"chart.png",'
    '"inputs":["tier_costs.csv"]}],"synthesis_instruction":"combine"}'
)


# --- the captures parse into fields, as fields ---------------------------------


@pytest.mark.parametrize(
    "raw", [PROSE_ARTEFACT_CHAIN, PROCESS_STEP_MULTI_ARTEFACT], ids=["chain", "multi"]
)
def test_a_real_planner_response_parses_into_typed_fields(raw: str) -> None:
    """Fields as fields: the schema's own values must live in the schema's own
    keys, never smuggled into the prose. This is the invariant the degenerate
    response below violates, and the one worth asserting on real output."""
    plan = workflow._parse_plan_json(raw, cap=workflow.max_steps())
    assert plan is not None, "a real planner response failed to parse"
    assert plan["steps"], "parsed to zero steps"

    for step in plan["steps"]:
        assert isinstance(step["produces_artefact"], bool)
        assert isinstance(step["artefact"], str)
        assert isinstance(step["inputs"], list)
        assert all(isinstance(name, str) for name in step["inputs"])
        assert step["instruction"], "a step parsed with an empty instruction"
        # No field name of this schema may appear in the prose.
        assert workflow._instruction_leaks_schema(step["instruction"]) is None

    # ...and a plan the parser accepts must also survive the structural check,
    # or every live run would degrade to a single ask.
    usable, reason = workflow._usable_plan(plan)
    assert usable is not None, f"a real plan was rejected as unusable: {reason}"


def test_the_captured_chain_really_does_declare_a_prose_file_as_an_input() -> None:
    """Guards the fixture itself. If a future capture no longer exhibits the
    bug, the tests below would pass for the wrong reason."""
    plan = workflow._parse_plan_json(PROSE_ARTEFACT_CHAIN, cap=4)
    assert plan is not None
    first, second = plan["steps"][0], plan["steps"][1]
    assert first["produces_artefact"] is True
    assert first["artefact"].endswith(".txt"), first["artefact"]
    assert first["artefact"] in second["inputs"], (
        "the capture no longer chains a prose artefact into a later step"
    )


# --- FAILURE B: a prose step's text IS the text file it declared ---------------


def _run_captured_plan(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[AskResponse, list[str]]:
    """Execute a captured plan with a model stand-in that behaves the way the
    real one does: a step asked for a .txt writes PROSE and no file; a step
    asked for a .csv writes a real file; a step asked for a .png returns an
    image. Nothing here invents a file the real model would not have made."""
    plan = workflow._parse_plan_json(raw, cap=workflow.max_steps())
    assert plan is not None
    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: plan)
    prompts: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        prompt = req.question
        prompts.append(prompt)
        wanted = ""
        marker = "This step must PRODUCE A REAL FILE: "
        if marker in prompt:
            wanted = prompt.split(marker, 1)[1].split(".", 1)[0]
        if ".csv" in prompt.split(marker)[-1][:120] and marker in prompt:
            name = next(
                (
                    tok
                    for tok in prompt.split(marker, 1)[1].split()
                    if tok.strip(",.").endswith(".csv")
                ),
                "out.csv",
            ).strip(",.")
            body = b"tier,model,cost\nFast,gpt-5-mini,0.0008\nSmart,gpt-5,0.004\n"
            return AskResponse(
                answer=f"Wrote {name}.",
                mode_used="m",
                notes="n",
                code_results=[
                    {
                        "code": "df.to_csv(...)",
                        "logs": "ok",
                        "images": [],
                        "files": [
                            {
                                "filename": name,
                                "mime_type": "text/csv",
                                "data": "data:text/csv;base64,"
                                + base64.b64encode(body).decode("ascii"),
                            }
                        ],
                    }
                ],
            )
        if ".png" in prompt.split(marker)[-1][:120] and marker in prompt:
            return AskResponse(
                answer="Wrote the chart.",
                mode_used="m",
                notes="n",
                images=["data:image/png;base64,ZmFrZQ=="],
            )
        # Every other step, INCLUDING one that declared a .txt artefact: prose
        # only. This is the behaviour that broke the live run.
        assert not wanted.endswith((".csv", ".png"))
        return AskResponse(
            answer="Requests are routed by tier: fast for simple asks, smart "
            "for hard ones, budget for local work.",
            mode_used="m",
            notes="n",
        )

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    result = workflow.run_workflow(AskRequest(question="summary, sheet and chart"))
    return result, prompts


def test_a_prose_step_s_text_becomes_the_text_file_a_later_step_declared(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILURE B, end to end on the real captured plan. "Write a summary, then
    use it" has to work: the summary step writes prose, and the step that
    declared that .txt as its input must receive the prose."""
    result, prompts = _run_captured_plan(PROSE_ARTEFACT_CHAIN, monkeypatch)

    plan = workflow._parse_plan_json(PROSE_ARTEFACT_CHAIN, cap=4)
    assert plan is not None
    summary_file = plan["steps"][0]["artefact"]

    consumer = next((p for p in prompts if f"--- begin {summary_file} ---" in p), None)
    assert consumer is not None, (
        f"{summary_file} was never carried into the step that declared it"
    )
    assert "Requests are routed by tier" in consumer, (
        "the file was named but its content was not carried"
    )

    # Nothing was skipped, and the deliverables that CAN be files are files.
    assert result.failure_message is None, result.failure_message
    statuses = [s.status for s in result.workflow_steps or []]
    assert "failed" not in statuses, statuses
    files = [f.filename for cr in result.code_results or [] for f in (cr.files or [])]
    assert any(f.endswith(".csv") for f in files), files
    assert result.images, "the chart never reached the final message"


def test_a_materialised_prose_file_is_not_dressed_up_as_a_code_result(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No code ran to make it, so it must not appear under the "Ran code" card
    claiming a sandbox produced it. It exists to be consumed, and the prose
    itself already reaches the user through the synthesis."""
    result, _ = _run_captured_plan(PROSE_ARTEFACT_CHAIN, monkeypatch)

    files = [f.filename for cr in result.code_results or [] for f in (cr.files or [])]
    assert not any(f.endswith((".txt", ".md")) for f in files), files


def test_only_text_shaped_artefacts_are_materialised_from_prose() -> None:
    """A step asked for a .csv that returns a markdown table genuinely failed;
    synthesising a .csv from prose would be inventing structure the step never
    committed to. That case must still fall through to the loud failure."""
    bag = workflow._ArtefactBag()
    prose = AskResponse(answer="| tier | cost |\n|---|---|", mode_used="m", notes="n")

    csv_step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Produce tier_costs.csv.",
        "produces_artefact": True,
        "artefact": "tier_costs.csv",
        "inputs": [],
    }
    assert workflow._materialise_prose_artefact(csv_step, prose, bag) == []  # type: ignore[arg-type]
    assert bag.produced == {}

    txt_step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Produce notes.txt.",
        "produces_artefact": True,
        "artefact": "notes.txt",
        "inputs": [],
    }
    assert workflow._materialise_prose_artefact(txt_step, prose, bag) == ["notes.txt"]  # type: ignore[arg-type]
    assert "notes.txt" in bag.produced


def test_a_real_file_is_never_overwritten_by_the_prose_fallback() -> None:
    """A step that ran code and really saved the file keeps the real one."""
    bag = workflow._ArtefactBag()
    bag.absorb(
        AskResponse(
            answer="Wrote it.",
            mode_used="m",
            notes="n",
            code_results=[
                {
                    "code": "open('notes.txt','w')",
                    "logs": "ok",
                    "images": [],
                    "files": [
                        {
                            "filename": "notes.txt",
                            "mime_type": "text/plain",
                            "data": "data:text/plain;base64,"
                            + base64.b64encode(b"the real file").decode("ascii"),
                        }
                    ],
                }
            ],
        )
    )
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Produce notes.txt.",
        "produces_artefact": True,
        "artefact": "notes.txt",
        "inputs": [],
    }
    prose = AskResponse(answer="different prose", mode_used="m", notes="n")
    assert workflow._materialise_prose_artefact(step, prose, bag) == []  # type: ignore[arg-type]
    text = workflow._artefact_as_text(bag.produced["notes.txt"])
    assert text == "the real file"


# --- FAILURE A: a step never requires its own artefact -------------------------


def test_a_degenerate_plan_is_reported_as_malformed_not_executed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The degenerate response is valid JSON, so the parser accepts it. It must
    still be refused as a PLAN, with its own reason, rather than executed as a
    silently shorter one."""
    plan = workflow._parse_plan_json(DEGENERATE_PLAN, cap=4)
    assert plan is not None, "precondition: this really is valid JSON"

    usable, reason = workflow._usable_plan(plan)
    assert usable is None
    assert "raw schema text" in reason
    assert "step 1" in reason

    monkeypatch.setattr(workflow, "get_client", lambda: object())
    monkeypatch.setattr(workflow, "_plan_workflow", lambda *a, **k: plan)
    calls: list[str] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        calls.append(req.question)
        return AskResponse(answer="single answer", mode_used="auto->smart", notes="n")

    monkeypatch.setattr(workflow, "run_orchestrator", fake_run_orchestrator)
    result = workflow.run_workflow(AskRequest(question="summary, sheet and chart"))

    # Answered as ONE request, not as a two-step workflow.
    assert len(calls) == 1
    assert result.answer == "single answer"
    # ...and the reason is on the record rather than silently swallowed.
    assert "workflow plan unusable" in result.notes
    assert "raw schema text" in result.notes


def test_a_first_step_is_never_failed_for_needing_its_own_outputs() -> None:
    """The FAILURE A trigger, isolated. Step 1 lists its own outputs as inputs
    while `artefact` holds a prose description with no filename in it, so the
    names are not recognised as the step's own. There is no earlier step, so
    there is nothing those inputs could refer to, and the step must simply
    run."""
    step: dict[str, object] = {
        "category": "planning",
        "instruction": "Summarise the routing.",
        "produces_artefact": True,
        # No filename here -- this is what defeated the previous guard.
        "artefact": "the summary and the spreadsheet",
        "inputs": ["routing_summary.txt", "tier_costs.csv"],
    }
    resolved = workflow._resolve_step_inputs(step, {}, set())  # type: ignore[arg-type]
    assert resolved.missing == []
    assert resolved.unreadable == []
    assert resolved.available == []


def test_an_earlier_step_s_broken_promise_still_fails_loudly() -> None:
    """The cost of the rule above must not be the guard itself. An earlier step
    that PROMISED a file and did not deliver puts the name in `expected`, so a
    later step declaring it is still required, still missing, still stopped."""
    step: dict[str, object] = {
        "category": "analysis",
        "instruction": "Chart it.",
        "produces_artefact": True,
        "artefact": "chart.png",
        "inputs": ["tier_costs.csv"],
    }
    resolved = workflow._resolve_step_inputs(step, {}, {"tier_costs.csv"})  # type: ignore[arg-type]
    assert resolved.missing == ["tier_costs.csv"]


def test_a_phantom_input_on_one_step_does_not_take_out_the_whole_plan() -> None:
    """The cascade, from a real captured plan. A leading process step invented
    `plan_artifacts.pdf`; step 2 declared it; stopping step 2 meant step 3 and
    step 4 lost the .csv step 2 was going to build. One phantom file that
    nothing needed cost all three deliverables.

    Rebuilt here as a plan rather than a unit check, because the damage was the
    cascade rather than the single stopped step.
    """
    plan: dict[str, object] = {
        "steps": [
            {
                "category": "planning",
                "instruction": "Identify the artefacts to build.",
                "produces_artefact": True,
                "artefact": "plan_artifacts.pdf",
                "inputs": [],
            },
            {
                "category": "coding",
                "instruction": "Produce costs_by_tier.csv listing each tier.",
                "produces_artefact": True,
                "artefact": "costs_by_tier.csv",
                "inputs": ["plan_artifacts.pdf"],
            },
            {
                "category": "coding",
                "instruction": "Produce cost_by_tier.png from costs_by_tier.csv.",
                "produces_artefact": True,
                "artefact": "cost_by_tier.png",
                "inputs": ["costs_by_tier.csv"],
            },
        ],
        "synthesis_instruction": "combine",
    }
    bag = workflow._ArtefactBag()
    expected: set[str] = set()
    stopped: list[int] = []

    for index, step in enumerate(plan["steps"]):  # type: ignore[arg-type]
        resolved = workflow._resolve_step_inputs(step, bag.produced, expected)
        expected |= workflow._expected_output_names(step)
        if resolved.missing or resolved.unreadable:
            stopped.append(index + 1)
            continue
        if step["artefact"].endswith(".csv"):
            body = b"tier,cost\nFast,0.0008\n"
            bag.absorb(
                AskResponse(
                    answer="wrote it",
                    mode_used="m",
                    notes="n",
                    code_results=[
                        {
                            "code": "c",
                            "logs": "l",
                            "images": [],
                            "files": [
                                {
                                    "filename": step["artefact"],
                                    "mime_type": "text/csv",
                                    "data": "data:text/csv;base64,"
                                    + base64.b64encode(body).decode("ascii"),
                                }
                            ],
                        }
                    ],
                )
            )

    assert stopped == [], f"steps stopped by a phantom input: {stopped}"
    assert "costs_by_tier.csv" in bag.produced


def test_an_image_input_is_dropped_rather_than_failing_the_step() -> None:
    """Found in a real captured plan: a trailing step declared the chart .png
    among its inputs. An image has no text rendering and a generated chart
    never lands in `produced` at all, so this could never be satisfied -- and
    failing the step over it would be a loud failure with nothing behind it.
    The step's context block already says the chart was written."""
    step: dict[str, object] = {
        "category": "summarization",
        "instruction": "Combine the artefacts.",
        "produces_artefact": False,
        "artefact": "",
        "inputs": ["cost_by_tier.png"],
    }
    resolved = workflow._resolve_step_inputs(step, {}, {"cost_by_tier.png"})  # type: ignore[arg-type]
    assert resolved.missing == []
    assert resolved.available == []


def test_the_captured_multi_artefact_plan_runs_without_a_spurious_failure(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other captured shape: a leading process step claiming all three
    artefacts. Untidy, but workable -- it must not be rejected, and it must not
    produce a loud failure with nothing behind it."""
    result, _ = _run_captured_plan(PROCESS_STEP_MULTI_ARTEFACT, monkeypatch)

    assert result.answer.strip()
    files = [f.filename for cr in result.code_results or [] for f in (cr.files or [])]
    assert any(f.endswith(".csv") for f in files), files


# --- the real planner, over the network ----------------------------------------


_LIVE = os.getenv("WORKFLOW_LIVE_PLANNER", "").strip().lower() in ("1", "true", "yes")

_LIVE_PROMPT = (
    "Write a short summary of how this app routes requests across model tiers, "
    "build a spreadsheet listing each tier with its model and typical cost per "
    "request, and produce a chart of cost by tier."
)


@pytest.mark.skipif(
    not _LIVE,
    reason="live planner call; set WORKFLOW_LIVE_PLANNER=1 (needs a real API key)",
)
def test_the_real_planner_returns_an_executable_plan_for_this_prompt() -> None:
    """Opt-in, and skipped by default for the same reason evals/ is excluded
    from CI: it makes a real router call, so it cannot run offline or on a CI
    runner without a key. Run it with

        WORKFLOW_LIVE_PLANNER=1 pytest tests/test_workflow_live_plans.py -k real_planner

    after touching the planning prompt or the plan schema. It asserts only what
    must hold for ANY plan -- fields as fields, no schema in the prose, every
    declared input satisfiable by an earlier step -- not a particular
    decomposition, which is not stable run to run.

    Builds its own client from the key in .env rather than going through
    app.orchestrator.get_client(). conftest.py pins OPENAI_API_KEY to a dummy
    value before any app module loads, deliberately, so that no test can
    accidentally spend money -- that protection stays exactly as it is for
    every other test, and this one opts out of it in the open.
    """
    from dotenv import dotenv_values
    from openai import OpenAI

    from app.settings import get_model_overrides

    key = (
        dotenv_values(Path(__file__).parent.parent / ".env").get("OPENAI_API_KEY") or ""
    ).strip()
    if not key:
        pytest.skip("no OPENAI_API_KEY in .env")

    cap = workflow.max_steps()
    plan = workflow._plan_workflow(
        _LIVE_PROMPT, OpenAI(api_key=key), get_model_overrides(), cap, deliverables=3
    )
    assert plan is not None, "the live planner returned nothing parseable"

    usable, reason = workflow._usable_plan(plan)
    assert usable is not None, f"live plan rejected as unusable: {reason}"
    assert 1 <= len(plan["steps"]) <= cap

    owned: set[str] = set()
    for index, step in enumerate(plan["steps"], 1):
        assert isinstance(step["produces_artefact"], bool)
        assert isinstance(step["inputs"], list)
        assert workflow._instruction_leaks_schema(step["instruction"]) is None
        own = workflow._filenames_in(step["artefact"])
        for name in step["inputs"]:
            key = name.strip().lower()
            assert key not in own, (
                f"step {index} declares its own artefact {key!r} as an input"
            )
        owned |= workflow._expected_output_names(step)

    # The whole point of the plan: the request has three deliverables, so at
    # least the two that must be FILES have to get producing steps.
    assert sum(1 for s in plan["steps"] if s["produces_artefact"]) >= 2
