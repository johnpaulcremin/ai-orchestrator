"""Opt-in multi-step workflow mode (mode="workflow"; see app/schemas.py's
Mode.workflow). Never the default — only engaged when the caller explicitly
asks for it.

A single cheap planning call (the same OPENAI_MODEL_ROUTER model/structured-
output plumbing app/routing.py's classifier uses) decomposes the request
into up to `max_steps()` focused sub-instructions (each tagged with a task
category from app/categories.py) plus a synthesis instruction describing how
to combine them into one final answer. An unparseable/failed plan falls back
to a normal single ask — this must never surface as an error.

Each sub-instruction then runs through the EXISTING single-ask pipeline
(run_orchestrator/stream_orchestrator — routing, role prompts, tools,
caching, and per-call budget gating all apply exactly as they do for any
other ask), with every prior step's instruction+answer folded in as context
for later steps. The final synthesis step is executed the same way,
streamed to the client for a normal-feeling response.

Budget: the WHOLE workflow's worst case (steps × per-step output cap,
priced against the smart-tier model as a conservative upper bound) is
reserved atomically up front via budget.reserve_workflow() — refusing before
any model call if it fails, identical UX to the ordinary daily-cap refusal.
Each step's own (much smaller) real cost is separately reserved/finalized by
its own run_orchestrator/stream_orchestrator call as usual; once every step
completes, the workflow-level placeholder is released (see
budget.reserve_workflow's docstring for why this correctly reconciles down
rather than double-counting).
"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any, TypedDict

from openai import BadRequestError

from . import budget
from .categories import ALL_CATEGORIES
from .orchestrator import get_client, run_orchestrator, stream_orchestrator
from .schemas import AskRequest, AskResponse, Mode, WorkflowStep
from .settings import get_model_overrides, model_setting
from .telemetry import logger

__all__ = ["max_steps", "run_workflow", "stream_workflow"]


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# The hard ceiling on planned steps regardless of WORKFLOW_MAX_STEPS — a
# runaway plan (or a misconfigured env var) can never blow past this, since
# it directly bounds the worst-case budget reservation and how many model
# calls one workflow can make.
_HARD_STEP_CAP = 6


def max_steps() -> int:
    """WORKFLOW_MAX_STEPS, clamped to [1, _HARD_STEP_CAP]. Does NOT include
    the synthesis step, which always runs in addition to these."""
    value = _int_env("WORKFLOW_MAX_STEPS", 4)
    if value < 1:
        return 4
    return min(value, _HARD_STEP_CAP)


def step_max_output_tokens() -> int:
    """Per-step output token cap — deliberately tighter than a normal
    smart-tier answer's budget, since a workflow step answers one focused
    sub-instruction rather than a whole request."""
    value = _int_env("WORKFLOW_STEP_MAX_OUTPUT_TOKENS", 1500)
    return value if value > 0 else 1500


# A workflow step's category is always one of the real task categories (the
# plan is constrained to them via the JSON schema below); synthesis has no
# natural category of its own, so it's tagged with the closest fit —
# "summarization" (condense/combine provided text) is exactly what the
# synthesis step does.
_SYNTHESIS_CATEGORY = "summarization"

_DEFAULT_SYNTHESIS_INSTRUCTION = (
    "Combine the results of every step above into one clear, complete answer "
    "to the user's original request."
)


class PlanStep(TypedDict):
    category: str
    instruction: str


class WorkflowPlan(TypedDict):
    steps: list[PlanStep]
    synthesis_instruction: str


_WORKFLOW_PLAN_PROMPT = """You are a planning assistant for an AI orchestrator. \
Break the user's request below into an ordered sequence of focused \
sub-instructions, each answered independently, then combined into one final \
answer. Reply with ONLY a JSON object, no other text:

{{"steps": [{{"category": "<one of: {categories}>", "instruction": "<a specific, self-contained instruction for this step>"}}],
 "synthesis_instruction": "<how to combine the step outputs into one final answer>"}}

Rules:
- Only split into multiple steps when the request GENUINELY has multiple
  distinct parts that benefit from being answered separately (e.g. "research
  X, then compare it to Y, then draft a recommendation"). A single simple
  question should get exactly one step.
- At most {max_steps} steps.
- Each step's instruction must be answerable on its own — it will be given
  the user's ORIGINAL request plus every PRIOR step's own instruction and
  answer as context, but never any LATER step's content.
- category must be exactly one of the listed task categories.
- synthesis_instruction describes how to combine every step's answer into
  the single final answer to the user's original request.

User's request:
{question}"""


_PLAN_FORMAT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": sorted(ALL_CATEGORIES)},
                    "instruction": {"type": "string"},
                },
                "required": ["category", "instruction"],
                "additionalProperties": False,
            },
        },
        "synthesis_instruction": {"type": "string"},
    },
    "required": ["steps", "synthesis_instruction"],
    "additionalProperties": False,
}


def _plan_format() -> dict[str, object]:
    return {
        "format": {
            "type": "json_schema",
            "name": "workflow_plan",
            "strict": True,
            "schema": _PLAN_FORMAT_SCHEMA,
        }
    }


def _parse_plan_json(raw: str, cap: int) -> WorkflowPlan | None:
    """Same tolerant-strip-then-strict-parse shape as
    routing._parse_classifier_json — an unparseable/malformed/empty plan
    returns None so the caller can fall back to a normal single ask rather
    than erroring."""
    text = (raw or "").strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    steps: list[PlanStep] = []
    for item in raw_steps[:cap]:
        if not isinstance(item, dict):
            return None
        category = str(item.get("category", "")).strip().lower()
        instruction = str(item.get("instruction", "")).strip()
        if category not in ALL_CATEGORIES or not instruction:
            return None
        steps.append({"category": category, "instruction": instruction})

    if not steps:
        return None

    synthesis_instruction = str(data.get("synthesis_instruction", "")).strip()
    return {"steps": steps, "synthesis_instruction": synthesis_instruction}


def _plan_workflow(
    question: str, client: object, overrides: dict[str, str] | None, cap: int
) -> WorkflowPlan | None:
    """Ask a small, cheap model to plan the workflow. Returns None on any
    failure (unsupported params, timeout, rate limit, unparseable output) —
    the exact same degrade-gracefully shape as routing._classify_with_ai,
    which this deliberately mirrors."""
    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano", overrides)
    prompt = _WORKFLOW_PLAN_PROMPT.format(
        categories=", ".join(sorted(ALL_CATEGORIES)),
        max_steps=cap,
        question=question[:4000],
    )

    timeout_client = client.with_options(timeout=20.0)  # type: ignore[attr-defined]

    def _create(**extra: object) -> object:
        return timeout_client.responses.create(
            model=router_model,
            input=prompt,
            max_output_tokens=1200,
            **extra,
        )

    attempts: tuple[dict[str, object], ...] = (
        {"text": _plan_format(), "reasoning": {"effort": "minimal"}},
        {"text": _plan_format()},
        {"reasoning": {"effort": "minimal"}},
        {},
    )

    result = None
    for kwargs in attempts:
        try:
            result = _create(**kwargs)
            break
        except BadRequestError:
            logger.warning(
                "workflow.plan_param_rejected model=%s params=%s",
                router_model,
                sorted(kwargs),
            )
            continue
        except Exception as err:
            logger.warning(
                "workflow.plan_failed model=%s err=%s", router_model, type(err).__name__
            )
            return None

    if result is None:
        logger.warning("workflow.plan_all_attempts_failed model=%s", router_model)
        return None

    raw = getattr(result, "output_text", None) or ""
    parsed = _parse_plan_json(raw, cap)

    if parsed is None:
        logger.warning("workflow.plan_unparseable output=%r", raw[:200])

    return parsed


def _worst_case_model(overrides: dict[str, str]) -> str:
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    return model_setting("OPENAI_MODEL_SMART", base, overrides)


def _mode_tag(step_count: int, auto_routed: bool) -> str:
    """`workflow(3 steps)` when the user picked the mode, `auto->workflow(3
    steps)` when the router did — so an automatic decision reads like every
    other routing decision in the mode badge (`auto->fast`, `auto->clarify`).
    Deliberately keeps the step count in both: it is the single most useful
    number for judging whether the decision was a good one."""
    prefix = "auto->" if auto_routed else ""
    return f"{prefix}workflow({step_count} steps)"


def _single_shot_fallback(
    req: AskRequest, owner: str | None, fallback_category: str | None
) -> AskResponse:
    """Answer as an ordinary single ask instead of a workflow.

    `forced_category`/`allow_auto_workflow=False` together make this both
    cheap and safe: the category the router already decided is reused rather
    than re-classified (one classification per request), and the orchestrator
    is told not to auto-route this into a workflow again, which would
    otherwise be an infinite loop — auto-workflow delegates INTO here, and
    this path re-enters the orchestrator.
    """
    return run_orchestrator(
        _fallback_request(req),
        owner=owner,
        forced_category=fallback_category,
        allow_auto_workflow=False,
    )


def _single_shot_fallback_stream(
    req: AskRequest, owner: str | None, fallback_category: str | None
) -> Generator[dict[str, Any], None, None]:
    """Streaming twin of _single_shot_fallback — same reuse-the-classification
    and no-recursion guarantees."""
    yield from stream_orchestrator(
        _fallback_request(req),
        owner=owner,
        forced_category=fallback_category,
        allow_auto_workflow=False,
    )


def _fallback_request(req: AskRequest) -> AskRequest:
    """A normal, non-workflow AskRequest for when planning fails — carries
    over everything from the original request except the mode itself."""
    return AskRequest(
        question=req.question,
        mode=Mode.auto,
        no_cache=req.no_cache,
        model=req.model,
        images=req.images,
        files=req.files,
        research=req.research,
    )


def _step_prompt(
    question: str, instruction: str, index: int, total: int, context: list[str]
) -> str:
    lines = [
        f"You are completing step {index + 1} of {total} in a larger multi-step task.",
        "The user's overall request was:",
        question,
    ]
    if context:
        lines.append("")
        lines.append("Prior steps completed so far:")
        lines.extend(context)
    lines.append("")
    lines.append("Your specific instruction for THIS step only:")
    lines.append(instruction)
    lines.append("")
    lines.append(
        "Answer only this step's instruction — do not attempt to answer the "
        "whole original request or restate earlier steps; a later synthesis "
        "step will combine everything into one final answer."
    )
    return "\n".join(lines)


def _synthesis_prompt(
    question: str, synthesis_instruction: str, context: list[str]
) -> str:
    lines = [
        "You are producing the FINAL answer to a multi-step task by combining "
        "the results of every step completed so far.",
        "The user's original request was:",
        question,
        "",
        "Steps completed:",
        *context,
        "",
        f"Synthesis instructions: {synthesis_instruction}",
        "",
        "Write the single final answer to the user's original request now — "
        "do not describe or list the steps, just give the complete answer.",
    ]
    return "\n".join(lines)


def _context_block(
    index: int, category: str, instruction: str, answer: str | None, failed: bool
) -> str:
    if failed:
        result = f"[This step failed — {answer or 'no answer produced'}]"
    else:
        result = answer or ""
    return f"Step {index + 1} ({category}): {instruction}\nResult: {result}"


def run_workflow(
    req: AskRequest,
    owner: str | None = None,
    auto_routed: bool = False,
    fallback_category: str | None = None,
) -> AskResponse:
    """Plan + execute a workflow request, non-streaming. Never returns an
    error for an unparseable plan — falls back to a normal single ask.

    `auto_routed` marks a workflow the ROUTER chose rather than the user
    (see routing.auto_workflow_enabled): it tags every mode_used with an
    `auto->` prefix so the decision is visible in the mode badge like any
    other routing decision, and it turns a budget refusal into the same
    single-shot fallback an unparseable plan already gets — degrade, never
    refuse, since the user never asked for a workflow in the first place and
    a plain answer is still a useful answer.

    `fallback_category` is the category the router already classified this
    question as. Threading it into the fallback keeps the "one classification
    per request" rule: without it, falling back would re-enter the
    orchestrator in auto mode and pay for a SECOND classifier call on a
    question that has already been classified once.
    """
    try:
        client = get_client()
    except RuntimeError as e:
        return AskResponse(answer="", mode_used=_mode_tag(0, auto_routed), notes=str(e))

    overrides = get_model_overrides()
    cap = max_steps()
    plan = _plan_workflow(req.question, client, overrides, cap)
    if plan is None:
        return _single_shot_fallback(req, owner, fallback_category)

    steps = plan["steps"]
    synthesis_instruction = (
        plan["synthesis_instruction"] or _DEFAULT_SYNTHESIS_INSTRUCTION
    )
    total_calls = len(steps) + 1  # +1 for synthesis

    worst_model = _worst_case_model(overrides)
    refusal, reservation_id = budget.reserve_workflow(
        worst_model, step_max_output_tokens(), total_calls, req.question, owner=owner
    )
    if refusal is not None:
        if auto_routed:
            # The user asked a question, not for a workflow — a plain answer
            # they can afford beats a refusal they did not ask for. The
            # single-shot path reserves its own (far smaller) budget, so this
            # can still succeed where the whole-workflow worst case could not.
            logger.info(
                "workflow.auto_budget_fallback steps=%d owner=%s", len(steps), owner
            )
            return _single_shot_fallback(req, owner, fallback_category)
        return AskResponse(
            answer="", mode_used=_mode_tag(len(steps), auto_routed), notes=refusal
        )

    step_records: list[WorkflowStep] = []
    context: list[str] = []
    any_failed = False

    for index, step in enumerate(steps):
        step_req = AskRequest(
            question=_step_prompt(
                req.question, step["instruction"], index, len(steps), context
            ),
            mode=Mode.auto,
            no_cache=True,
        )
        result = run_orchestrator(
            step_req, owner=owner, forced_category=step["category"]
        )
        ok = bool(result.answer.strip())
        if not ok:
            any_failed = True
        step_records.append(
            WorkflowStep(
                category=step["category"],
                instruction=step["instruction"],
                model=result.model or "",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                status="ok" if ok else "failed",
            )
        )
        context.append(
            _context_block(
                index,
                step["category"],
                step["instruction"],
                result.answer or result.notes,
                not ok,
            )
        )

    synthesis_req = AskRequest(
        question=_synthesis_prompt(req.question, synthesis_instruction, context),
        mode=Mode.auto,
        no_cache=True,
    )
    synthesis_result = run_orchestrator(
        synthesis_req, owner=owner, forced_category=_SYNTHESIS_CATEGORY
    )
    synthesis_ok = bool(synthesis_result.answer.strip())
    step_records.append(
        WorkflowStep(
            category=_SYNTHESIS_CATEGORY,
            instruction=synthesis_instruction,
            model=synthesis_result.model or "",
            input_tokens=synthesis_result.input_tokens,
            output_tokens=synthesis_result.output_tokens,
            cost_usd=synthesis_result.cost_usd,
            status="ok" if synthesis_ok else "failed",
        )
    )

    budget.release(reservation_id)

    total_input = sum(s.input_tokens or 0 for s in step_records) or None
    total_output = sum(s.output_tokens or 0 for s in step_records) or None
    total_cost = sum(s.cost_usd or 0.0 for s in step_records) or None

    answer = synthesis_result.answer.strip()
    if not answer:
        # Synthesis itself failed — surface the completed steps' own answers
        # rather than an empty final answer (the "surface partial results"
        # guardrail).
        answer = "\n\n".join(context) or "The workflow could not produce an answer."
        any_failed = True

    notes = (
        f"Workflow: {len(steps)} step(s) + synthesis"
        f"{' (some steps failed — see workflow_steps)' if any_failed else ''}"
    )
    return AskResponse(
        answer=answer,
        mode_used=_mode_tag(len(steps), auto_routed),
        notes=notes,
        model=synthesis_result.model,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_usd=total_cost,
        workflow_steps=step_records,
    )


def stream_workflow(
    req: AskRequest,
    owner: str | None = None,
    auto_routed: bool = False,
    fallback_category: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Streaming variant of run_workflow — see it for `auto_routed` and
    `fallback_category`.

    Sub-steps run non-streamed (run_orchestrator) — they're intermediate
    working answers, not what the user actually reads — with a "step" event
    emitted before and after each one so the client can show progress. Only
    the final synthesis step is delta-streamed (stream_orchestrator), for
    the same responsive feel as an ordinary streamed answer. An unparseable
    plan delegates the WHOLE generator to an ordinary stream_orchestrator
    call, so the client sees a completely normal streamed answer — no
    workflow-specific events at all — rather than an error.
    """
    try:
        client = get_client()
    except RuntimeError as e:
        yield {"event": "error", "data": {"message": str(e)}}
        return

    overrides = get_model_overrides()
    cap = max_steps()
    plan = _plan_workflow(req.question, client, overrides, cap)
    if plan is None:
        yield from _single_shot_fallback_stream(req, owner, fallback_category)
        return

    steps = plan["steps"]
    synthesis_instruction = (
        plan["synthesis_instruction"] or _DEFAULT_SYNTHESIS_INSTRUCTION
    )
    total_calls = len(steps) + 1
    mode_used = _mode_tag(len(steps), auto_routed)

    worst_model = _worst_case_model(overrides)
    refusal, reservation_id = budget.reserve_workflow(
        worst_model, step_max_output_tokens(), total_calls, req.question, owner=owner
    )
    if refusal is not None:
        if auto_routed:
            # See run_workflow's identical branch: the user asked a question,
            # not for a workflow, so degrade to a single answer they can
            # afford rather than surfacing a refusal they never invited.
            logger.info(
                "workflow.auto_budget_fallback steps=%d owner=%s", len(steps), owner
            )
            yield from _single_shot_fallback_stream(req, owner, fallback_category)
            return
        yield {"event": "error", "data": {"message": refusal}}
        return

    yield {
        "event": "meta",
        "data": {
            "mode_used": mode_used,
            "model": "",
            "notes": f"Planned {len(steps)} step(s) + synthesis",
        },
    }

    step_records: list[WorkflowStep] = []
    context: list[str] = []
    any_failed = False

    try:
        for index, step in enumerate(steps):
            yield {
                "event": "step",
                "data": {
                    "index": index,
                    "total": total_calls,
                    "category": step["category"],
                    "instruction": step["instruction"],
                    "status": "running",
                },
            }
            step_req = AskRequest(
                question=_step_prompt(
                    req.question, step["instruction"], index, len(steps), context
                ),
                mode=Mode.auto,
                no_cache=True,
            )
            result = run_orchestrator(
                step_req, owner=owner, forced_category=step["category"]
            )
            ok = bool(result.answer.strip())
            if not ok:
                any_failed = True
            step_records.append(
                WorkflowStep(
                    category=step["category"],
                    instruction=step["instruction"],
                    model=result.model or "",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                    status="ok" if ok else "failed",
                )
            )
            context.append(
                _context_block(
                    index,
                    step["category"],
                    step["instruction"],
                    result.answer or result.notes,
                    not ok,
                )
            )
            yield {
                "event": "step",
                "data": {
                    "index": index,
                    "total": total_calls,
                    "category": step["category"],
                    "instruction": step["instruction"],
                    "status": "ok" if ok else "failed",
                    "model": result.model,
                },
            }

        synthesis_index = len(steps)
        yield {
            "event": "step",
            "data": {
                "index": synthesis_index,
                "total": total_calls,
                "category": _SYNTHESIS_CATEGORY,
                "instruction": synthesis_instruction,
                "status": "running",
            },
        }

        accumulated: list[str] = []
        synthesis_model: str | None = None
        synthesis_tokens_in = 0
        synthesis_tokens_out = 0
        synthesis_cost = 0.0
        synthesis_req = AskRequest(
            question=_synthesis_prompt(req.question, synthesis_instruction, context),
            mode=Mode.auto,
            no_cache=True,
        )
        for event in stream_orchestrator(
            synthesis_req, owner=owner, forced_category=_SYNTHESIS_CATEGORY
        ):
            if event["event"] == "delta":
                text = str(event["data"].get("text", ""))
                accumulated.append(text)
                yield event
            elif event["event"] == "done":
                data = event["data"]
                synthesis_model = data.get("model")
                synthesis_tokens_in = int(data.get("input_tokens") or 0)
                synthesis_tokens_out = int(data.get("output_tokens") or 0)
                synthesis_cost = float(data.get("cost_usd") or 0.0)
            elif event["event"] == "error":
                any_failed = True

        answer_final = "".join(accumulated).strip()
        synthesis_ok = bool(answer_final)
        if not synthesis_ok:
            any_failed = True
            answer_final = (
                "\n\n".join(context) or "The workflow could not produce an answer."
            )

        step_records.append(
            WorkflowStep(
                category=_SYNTHESIS_CATEGORY,
                instruction=synthesis_instruction,
                model=synthesis_model or "",
                input_tokens=synthesis_tokens_in or None,
                output_tokens=synthesis_tokens_out or None,
                cost_usd=synthesis_cost or None,
                status="ok" if synthesis_ok else "failed",
            )
        )
        yield {
            "event": "step",
            "data": {
                "index": synthesis_index,
                "total": total_calls,
                "category": _SYNTHESIS_CATEGORY,
                "instruction": synthesis_instruction,
                "status": "ok" if synthesis_ok else "failed",
                "model": synthesis_model,
            },
        }

        budget.release(reservation_id)

        total_input = sum(s.input_tokens or 0 for s in step_records) or None
        total_output = sum(s.output_tokens or 0 for s in step_records) or None
        total_cost = sum(s.cost_usd or 0.0 for s in step_records) or None
        notes = (
            f"Workflow: {len(steps)} step(s) + synthesis"
            f"{' (some steps failed — see workflow_steps)' if any_failed else ''}"
        )

        yield {
            "event": "done",
            "data": {
                "answer": answer_final,
                "mode_used": mode_used,
                "notes": notes,
                "model": synthesis_model,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cost_usd": total_cost,
                "workflow_steps": [s.model_dump() for s in step_records],
            },
        }
    except GeneratorExit:
        # Client disconnected mid-workflow (Stop button, tab close) — release
        # the workflow-level placeholder (any already-completed step's real
        # spend was already recorded by its own run_orchestrator/
        # stream_orchestrator call, independent of this reservation) and
        # propagate the close, same discipline as _stream_and_persist's own
        # GeneratorExit handling.
        budget.release(reservation_id)
        raise
