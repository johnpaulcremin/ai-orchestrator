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

ARTEFACT INPUTS. Every step is a SEPARATE provider call and therefore gets a
SEPARATE code-execution sandbox — OpenAI's `container: {"type": "auto"}` mints
a fresh container per request and Anthropic's code_execution container is
likewise per-request, with no cross-call reuse either side. A file step N
wrote is consequently not on step N+1's filesystem, and a step told to work
"from the spreadsheet you just made" will find nothing there. Left to itself a
model does NOT report that: it searches, finds nothing, silently reconstructs
the data from its own recollection, and hands back a confident artefact whose
numbers disagree with the real one. So an artefact a later step needs is
carried into that step's own prompt as text (see _resolve_step_inputs /
_artefact_as_text), and a step whose declared input is genuinely unavailable
is FAILED OUTRIGHT before any model call rather than allowed to improvise —
the whole workflow still returns every other step's work (see
"partial results" below).

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

import base64
import binascii
import json
import os
import re
from collections.abc import Generator
from typing import Any, NamedTuple, TypedDict

from openai import BadRequestError

from . import budget
from .categories import ALL_CATEGORIES
from .orchestrator import (
    code_execution_capable_model,
    get_client,
    run_orchestrator,
    stream_orchestrator,
)
from .schemas import (
    _XLSX_MIME,
    AskRequest,
    AskResponse,
    CodeResult,
    Mode,
    WorkflowStep,
)
from .settings import get_model_overrides, model_setting
from .spreadsheet_ingestion import xlsx_to_text
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
    # True when this step's job is to PRODUCE a file the user asked for (a
    # spreadsheet, a chart image, a document) rather than to write prose about
    # it. Such a step is executed with code execution forced on and, if
    # necessary, moved to a model whose provider actually supports the tool —
    # its category tier alone is not enough, since a step routed to a
    # budget/LiteLLM model would silently lose the capability.
    produces_artefact: bool
    # What that file is, in the user's terms ("an .xlsx listing Q3 revenue by
    # region"). Empty for a prose step. Carried into the step prompt so the
    # instruction names the artefact, and into the synthesis prompt so the
    # final answer refers to the real file instead of re-rendering it.
    artefact: str
    # Filenames produced by EARLIER steps that this step must read — the
    # planner's explicit declaration of a cross-step data dependency. Each
    # one is materialised into this step's prompt as text before the step
    # runs, and one that cannot be found FAILS the step outright instead of
    # letting it invent the values (see _resolve_step_inputs). The planner
    # declaring the dependency is the primary signal; _resolve_step_inputs
    # also scans the instruction for a filename an earlier step really did
    # produce, so a plan that forgets to fill this in still gets the file.
    inputs: list[str]


class WorkflowPlan(TypedDict):
    steps: list[PlanStep]
    synthesis_instruction: str


_WORKFLOW_PLAN_PROMPT = """You are a planning assistant for an AI orchestrator. \
Break the user's request below into an ordered sequence of focused \
sub-instructions, each answered independently, then combined into one final \
answer. Reply with ONLY a JSON object, no other text:

{{"steps": [{{"category": "<one of: {categories}>", "instruction": "<a specific, self-contained instruction for this step>", "produces_artefact": <true|false>, "artefact": "<what file this step produces, INCLUDING its filename, or empty string>", "inputs": ["<filename produced by an earlier step that this step must read>"]}}],
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

ARTEFACTS — the most important rule. {deliverables_hint}
An ARTEFACT is a FILE the user asked to be handed: a spreadsheet (.xlsx/.csv),
a chart or diagram (an image), a document. Prose is not an artefact.

- EVERY artefact the user asked for MUST get its own step whose instruction is
  to actually PRODUCE that file — "produce an .xlsx file listing ...", "render
  a bar chart as a PNG image showing ...". Set produces_artefact: true and put
  a short description in artefact.
- Do NOT write process steps ("plan the approach", "decide what to fetch",
  "draft sub-answers about how to answer"). Those consume the step budget and
  hand back nothing. Steps exist to produce the answer, not to discuss it.
- A step that produces a file must SAY SO in its instruction, in the
  imperative, naming the file type.
- A step that only writes prose sets produces_artefact: false and artefact: "".
- Give every artefact an explicit FILENAME and put it in both the instruction
  and artefact ("produce tier_costs.csv listing ..."), so a later step can name
  the exact file it needs.

INPUTS — when one step needs another step's file. Each step runs in its OWN
fresh sandbox, so a file an earlier step wrote is NOT on a later step's
filesystem. If a later step must use the DATA from an earlier step's file
(charting the figures from a spreadsheet, summarising a generated CSV), list
that earlier step's exact filename in the later step's "inputs". The file's
contents are then handed to that step directly.
- inputs may only ever name a file an EARLIER step produces — never this
  step's own output, and never a file the user supplied.
- Leave inputs as [] when the step needs nothing from an earlier file.
- Do NOT write an instruction like "using the costs from the spreadsheet"
  without also listing that spreadsheet's filename in inputs. That is the one
  mistake that produces two attached files which disagree with each other.

User's request:
{question}"""

# Spliced into the plan prompt when the router already counted the artefacts
# (see routing._parse_classifier_json's `deliverables`) — the planner is told
# the number rather than left to re-derive it, which is exactly the step where
# the deliverables used to get lost.
_DELIVERABLES_HINT = (
    "The router counted {deliverables} distinct artefacts in this request; "
    "expect roughly that many artefact-producing steps."
)
_DELIVERABLES_HINT_UNKNOWN = (
    "Count the distinct artefacts the request asks for before planning."
)


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
                    "produces_artefact": {"type": "boolean"},
                    "artefact": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "category",
                    "instruction",
                    "produces_artefact",
                    "artefact",
                    "inputs",
                ],
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
        # Tolerant, and defaulting to "prose step": a model that rejected the
        # strict schema and free-formed its plan omits these, and a step
        # wrongly marked as artefact-producing costs a forced upgrade to a
        # pricier model for nothing.
        artefact = str(item.get("artefact", "")).strip()
        produces = bool(item.get("produces_artefact", False))
        # Same tolerance for `inputs`: a plan that omits it declares no
        # cross-step dependency, which is the safe reading — an input this
        # module cannot satisfy FAILS its step, so inventing one would turn a
        # sloppy plan into a hard error.
        raw_inputs = item.get("inputs")
        inputs = (
            [str(name).strip() for name in raw_inputs if str(name).strip()]
            if isinstance(raw_inputs, list)
            else []
        )
        steps.append(
            {
                "category": category,
                "instruction": instruction,
                "produces_artefact": produces,
                "artefact": artefact,
                "inputs": inputs,
            }
        )

    if not steps:
        return None

    synthesis_instruction = str(data.get("synthesis_instruction", "")).strip()
    return {"steps": steps, "synthesis_instruction": synthesis_instruction}


def _plan_workflow(
    question: str,
    client: object,
    overrides: dict[str, str] | None,
    cap: int,
    deliverables: int = 0,
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
        deliverables_hint=(
            _DELIVERABLES_HINT.format(deliverables=deliverables)
            if deliverables >= 2
            else _DELIVERABLES_HINT_UNKNOWN
        ),
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


def _worst_case_model(
    overrides: dict[str, str], steps: list[PlanStep] | None = None
) -> str:
    """The priciest model any step in this plan could plausibly resolve to —
    the conservative upper bound reserve_workflow() prices against.

    The smart-tier model is the normal answer, since any step's category could
    route that high. An ARTEFACT step changes this only when the smart tier
    cannot run code: it is forced onto a code-execution-capable model (see
    orchestrator._apply_code_execution_override), so pricing the smart tier
    would then be quoting a model the workflow will not use. Deliberately asks
    orchestrator.code_execution_capable_model rather than re-deriving the
    choice, so the reservation and the routing can never disagree.
    """
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    smart = model_setting("OPENAI_MODEL_SMART", base, overrides)
    if not steps or not any(s["produces_artefact"] for s in steps):
        return smart
    return code_execution_capable_model(smart) or smart


class _ArtefactBag:
    """Collects the real files/images produced across a workflow's steps.

    Before this, a step's artefacts were dropped on the floor: `code_results`
    appeared nowhere in this module, `WorkflowStep` had no field for them, and
    the final AskResponse omitted them — so a step could generate a genuine
    .xlsx and the user would still only ever see prose about it. Aggregating
    them onto the final response makes them render through the SAME frontend
    path a single-shot answer uses (attachment chip, inline .xlsx/.csv
    preview, inline image, and the collapsible "Ran code" transparency card),
    with no frontend change at all.
    """

    def __init__(self) -> None:
        self.code_results: list[dict[str, Any]] = []
        self.images: list[str] = []
        # filename (lowercased) -> the CodeFile dict for it, so a later step
        # can ask for an earlier step's artefact BY NAME. First writer wins:
        # if two steps somehow emit the same filename, the earlier one is the
        # one a later step's instruction was written against.
        self.produced: dict[str, dict[str, Any]] = {}

    def absorb(self, result: AskResponse) -> None:
        for entry in result.code_results or []:
            payload = entry if isinstance(entry, dict) else entry.model_dump()
            self.code_results.append(payload)
            for file in payload.get("files") or []:
                name = str(file.get("filename") or "").strip().lower()
                if name:
                    self.produced.setdefault(name, file)
        self.images.extend(result.images or [])

    def as_models(self) -> list[CodeResult] | None:
        """The same entries as CodeResult models, for AskResponse. Held as
        dicts internally because the streaming "done" event carries them
        straight out as JSON."""
        if not self.code_results:
            return None
        return [CodeResult.model_validate(entry) for entry in self.code_results]

    @property
    def files(self) -> list[dict[str, Any]]:
        """Every generated FILE across every step, flattened — the thing the
        synthesis step must not re-render as a markdown table."""
        out: list[dict[str, Any]] = []
        for entry in self.code_results:
            out.extend(entry.get("files") or [])
        return out

    def any_produced(self) -> bool:
        return bool(self.files or self.images)

    def describe(self) -> str:
        """Plain-English list of what actually exists, for the synthesis
        prompt. Empty when nothing was produced — which is what makes the
        no-markdown-substitution rule conditional."""
        names = [str(f.get("filename") or "a file") for f in self.files]
        if self.images:
            names.append(f"{len(self.images)} generated image(s)")
        return ", ".join(names)


# Filename-shaped tokens in a step's own wording, so a plan that describes the
# dependency in prose ("chart the costs from tier_costs.csv") but forgets to
# fill in `inputs` still gets the file. Deliberately no spaces in the stem: a
# greedier pattern turns "the file tier costs.csv" into one 15-character
# "filename" that matches nothing.
_FILENAME_RE = re.compile(
    r"[\w][\w\-.]*\.(?:csv|xlsx|xls|json|txt|md|tsv|png|jpg|jpeg|svg|pdf|docx)\b",
    re.IGNORECASE,
)

# Artefact types whose bytes can be handed to a later step as text. .xlsx goes
# through the SAME bounded sheet-to-text extraction an uploaded workbook
# already uses (spreadsheet_ingestion.xlsx_to_text), so a generated workbook
# and an attached one reach a model in identical shape.
_TEXT_ARTEFACT_MIMES = {"text/csv", "text/plain", "application/json"}

# Ceiling on how much carried-forward artefact text one step's prompt may
# absorb. xlsx_to_text is already bounded per sheet; this bounds the total
# across however many inputs a step declares, so a step prompt cannot grow
# without limit (AskRequest.question caps at 100k chars, and a step's own
# instruction still has to fit).
_MAX_INLINED_ARTEFACT_CHARS = 20_000


def _filenames_in(text: str) -> set[str]:
    return {match.group(0).lower() for match in _FILENAME_RE.finditer(text or "")}


def _match_produced(
    name: str, produced: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """The produced artefact `name` refers to, or None.

    Falls back to matching on the STEM alone, because the planner names the
    file before any of it exists: it writes "tier_costs.csv" into a later
    step's inputs while the producing step's model actually saves
    tier_costs.xlsx. Failing that step would be a false alarm — the data it
    needs is right there — so an unambiguous stem match counts. A stem that
    matches nothing still fails, loudly, which is the case that matters.
    """
    key = name.strip().lower()
    if key in produced:
        return produced[key]
    stem = key.rsplit(".", 1)[0]
    if not stem:
        return None
    for produced_name, file in produced.items():
        if produced_name.rsplit(".", 1)[0] == stem:
            return file
    return None


def _artefact_as_text(file: dict[str, Any]) -> str | None:
    """One produced artefact's content, rendered as text a later step's prompt
    can carry. None when this file's type has no text rendering here (a .docx,
    a .pdf, a generated image), which is treated as an unavailable input
    rather than papered over — see _resolve_step_inputs.
    """
    filename = str(file.get("filename") or "")
    mime = str(file.get("mime_type") or "")
    data = str(file.get("data") or "")
    if ";base64," not in data:
        return None
    try:
        raw = base64.b64decode(data.split(";base64,", 1)[1], validate=True)
    except (binascii.Error, ValueError):
        logger.warning("workflow.artefact_input_undecodable filename=%s", filename)
        return None
    if mime == _XLSX_MIME:
        try:
            return xlsx_to_text(raw, filename)
        except ValueError:
            logger.warning("workflow.artefact_input_unparseable filename=%s", filename)
            return None
    if mime in _TEXT_ARTEFACT_MIMES:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("workflow.artefact_input_not_utf8 filename=%s", filename)
            return None
    return None


class _StepInputs(NamedTuple):
    """What _resolve_step_inputs found for one step.

    `available` is (filename, text) per input that was found AND could be
    rendered; `missing` names inputs no earlier step produced; `unreadable`
    names inputs that exist but have no text rendering. Either failure list
    being non-empty stops the step — the two are kept apart only so the
    diagnostic can say which happened.
    """

    available: list[tuple[str, str]]
    missing: list[str]
    unreadable: list[str]


def _resolve_step_inputs(
    step: PlanStep, produced: dict[str, dict[str, Any]], expected: set[str]
) -> _StepInputs:
    """Work out which earlier-step artefacts this step needs, and fetch them.

    Two sources, with deliberately different strictness:

    * The planner's own `inputs` list is authoritative — every entry is
      required, and one that cannot be resolved fails the step. The planner
      said this step reads that file; if it isn't there, the step cannot do
      its job honestly.
    * Filenames merely SCANNED out of the instruction only count when an
      earlier step actually produced or promised that name. A scan can't tell
      "read tier_costs.csv" from "write tier_costs.csv", so treating an
      unrecognised scanned name as a required input would fail producing steps
      for naming their own output. Restricting the scan to names an earlier
      step is known to own makes it purely additive: it can find a file, never
      invent a failure.

    A filename in this step's own `artefact` is excluded outright — that's its
    output, not its input, even when an earlier step happens to share the name.
    """
    declared = {name.strip().lower() for name in step["inputs"] if name.strip()}
    own = _filenames_in(step["artefact"])
    scanned = (_filenames_in(step["instruction"]) - own) & (set(produced) | expected)
    required = sorted((declared | scanned) - own)

    available: list[tuple[str, str]] = []
    missing: list[str] = []
    unreadable: list[str] = []
    budget_left = _MAX_INLINED_ARTEFACT_CHARS

    for name in required:
        file = _match_produced(name, produced)
        if file is None:
            missing.append(name)
            continue
        text = _artefact_as_text(file)
        if text is None:
            unreadable.append(str(file.get("filename") or name))
            continue
        if len(text) > budget_left:
            text = (
                text[: max(budget_left, 0)]
                + "\n[truncated: the rest of this file did not fit]"
            )
        budget_left -= len(text)
        available.append((str(file.get("filename") or name), text))

    return _StepInputs(available, missing, unreadable)


def _missing_input_detail(
    index: int, step: PlanStep, resolved: _StepInputs, produced: dict[str, Any]
) -> str:
    """The RAW diagnostic for a step stopped by an unavailable input — the
    half that belongs in `notes`/the details disclosure, per the split
    established in 8bfc2b8. Names the step, what it wanted, and what actually
    existed at that point, so the failure can be traced without a re-run."""
    wanted: list[str] = []
    if resolved.missing:
        wanted.append(f"never produced: {', '.join(resolved.missing)}")
    if resolved.unreadable:
        wanted.append(f"no text rendering: {', '.join(resolved.unreadable)}")
    have = ", ".join(sorted(produced)) or "none"
    return (
        f"step {index + 1} ({step['category']}) needed an earlier step's file "
        f"({'; '.join(wanted)}); artefacts produced so far: {have}"
    )


def _missing_input_failure_message(details: list[str]) -> str:
    """The PLAIN-ENGLISH counterpart to _missing_input_detail, for the user —
    the other half of 8bfc2b8's split. Says what was skipped and why that is
    better than what would otherwise have happened, because "a step was
    skipped" on its own reads like a bug rather than a guard."""
    count = len(details)
    subject = "One step" if count == 1 else f"{count} steps"
    verb = "was" if count == 1 else "were"
    return (
        f"{subject} of this workflow {verb} skipped: {'it' if count == 1 else 'they'} "
        "needed a file an earlier step was supposed to produce, and that file "
        "was not available. The step was stopped rather than allowed to guess "
        "at the missing figures, which would have produced an attachment that "
        "quietly disagreed with the others. Everything else in the workflow "
        "still ran — see the details for exactly what was missing."
    )


def _mode_tag(total_steps: int, auto_routed: bool) -> str:
    """`workflow(5 steps)` when the user picked the mode, `auto->workflow(5
    steps)` when the router did — so an automatic decision reads like every
    other routing decision in the mode badge (`auto->fast`, `auto->clarify`).

    STEP-COUNT CONVENTION, used everywhere: `total_steps` counts every step
    that appears in the UI's own breakdown, INCLUDING synthesis. The badge
    used to report planned steps only (4) while the disclosure listed
    planned + synthesis (5), so the same workflow was labelled two different
    sizes on one message. Synthesis is a real, separately-billed model call
    with its own row in the breakdown, so counting it is both the more honest
    number and the one that matches what the reader can see. `workflow_steps`
    has exactly this length, and the streaming "step" events' `total` field
    already used it.
    """
    prefix = "auto->" if auto_routed else ""
    return f"{prefix}workflow({total_steps} steps)"


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
    question: str,
    instruction: str,
    index: int,
    total: int,
    context: list[str],
    artefact: str = "",
    inputs: list[tuple[str, str]] | None = None,
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
    if inputs:
        # The fix for the silent-disagreement bug. A step told to work "from
        # the spreadsheet" gets a fresh sandbox with no spreadsheet in it; the
        # observed failure was a model running `find / -iname ...`, finding
        # nothing, and then rebuilding the file from memory with different
        # numbers. Handing it the real bytes here removes the reason to guess,
        # and saying WHY the file isn't on disk removes the search.
        lines.append("")
        lines.append(
            "FILE CONTENTS FROM AN EARLIER STEP. Each step of this task runs "
            "in its own fresh sandbox, so these files are NOT on your "
            "filesystem — do not search for them. The exact contents are "
            "reproduced below. Use these values verbatim; do not recall, "
            "re-derive, round, or substitute your own. If you need the file "
            "itself, write this exact content to that filename first, then "
            "work from it."
        )
        for name, text in inputs:
            lines.append(f"--- begin {name} ---")
            lines.append(text)
            lines.append(f"--- end {name} ---")
    lines.append("")
    lines.append("Your specific instruction for THIS step only:")
    lines.append(instruction)
    lines.append("")
    if artefact:
        # An artefact step's entire purpose is the FILE. Saying so explicitly
        # matters: with code execution available but nothing asking for a file,
        # a model reliably answers in prose and never calls the tool — which is
        # exactly how a three-artefact request came back as a markdown table
        # and ASCII bars.
        lines.append(
            f"This step must PRODUCE A REAL FILE: {artefact}. Write and run "
            "code to generate it and save it to disk, so it comes back as an "
            "actual downloadable file. Do NOT print the contents as a markdown "
            "table, ASCII art, or a code block instead — a described file is a "
            "failed step. Keep any accompanying prose to one sentence."
        )
        # A caveat row is not data. A live run appended
        # `Note,All listed costs are illustrative examples, not live billing
        # data,` under a three-column header: the unquoted commas split it into
        # extra fields, so the file no longer parses under a strict CSV reader
        # and openpyxl/pandas read a ragged trailing row. The caveat itself was
        # worth saying — just not in the table.
        lines.append(
            "If the file is tabular (.csv/.xlsx), it must contain ONLY the "
            "data: exactly one header row, then data rows, every row with the "
            "same number of columns as the header. Never append a note, "
            "caveat, disclaimer, source line, or total as an extra row, and "
            "never leave a comma unquoted inside a field. Anything you want to "
            "say about the data belongs in your one sentence of prose, not in "
            "the file."
        )
        lines.append("")
    lines.append(
        "Answer only this step's instruction — do not attempt to answer the "
        "whole original request or restate earlier steps; a later synthesis "
        "step will combine everything into one final answer."
    )
    return "\n".join(lines)


def _synthesis_prompt(
    question: str,
    synthesis_instruction: str,
    context: list[str],
    artefacts: str = "",
    skipped: list[str] | None = None,
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
    ]
    if artefacts:
        # CONDITIONAL, and deliberately so. These files are already attached to
        # this very message, so re-rendering them as a markdown table just
        # duplicates something the user can already open. But when NO file was
        # produced — code execution off, or an artefact step that degraded to
        # text — a markdown table is the correct and only useful output, so
        # the prohibition must not apply then.
        lines.append(
            f"These files were produced by the steps and are ALREADY ATTACHED "
            f"to this answer: {artefacts}. Refer to them by name and say what "
            "each contains. Do NOT reproduce their contents as a markdown "
            "table, ASCII chart, or code block — the user already has the real "
            "files."
        )
        lines.append("")
    if skipped:
        # The counterpart to the "already attached" rule above, and the reason
        # the whole loud-failure path is worth having: a skipped step must not
        # be quietly filled in by the synthesis instead. Left unsaid, the model
        # sees a request for a chart, no chart, and a table of numbers in the
        # context — and helpfully draws the chart in ASCII, which puts the app
        # right back where it started.
        lines.append(
            "PART OF THIS REQUEST COULD NOT BE COMPLETED. The following steps "
            "were stopped because a file they needed from an earlier step was "
            "not available: " + "; ".join(skipped) + ". Say plainly in your "
            "answer which part could not be produced and why. Do NOT stand in "
            "for the missing work with your own figures, a markdown table, an "
            "ASCII chart, or an estimate, and do NOT write as though the "
            "missing file exists."
        )
        lines.append("")
    lines.append(
        "Write the single final answer to the user's original request now — "
        "do not describe or list the steps, just give the complete answer."
    )
    return "\n".join(lines)


def _expected_output_names(step: PlanStep) -> set[str]:
    """Filenames an artefact step has PROMISED to produce, taken from its
    `artefact` description only.

    Deliberately not scanned from the instruction as well: a producing step's
    instruction routinely names both its output and an input ("chart the costs
    from tier_costs.csv into cost_by_tier.png"), and adding the input to the
    promised-outputs set would let a still-later step treat it as an expected
    artefact that then reads as missing. `artefact` describes one thing — what
    this step hands back — so it is the only unambiguous source.
    """
    return _filenames_in(step["artefact"]) if step["produces_artefact"] else set()


def _failed_step_record(step: PlanStep) -> WorkflowStep:
    """The breakdown row for a step stopped before it ran. Zero tokens and no
    model, because no model call was made — a stopped step must not look like
    one that ran and came back empty."""
    return WorkflowStep(
        category=step["category"],
        instruction=step["instruction"],
        model="",
        status="failed",
    )


def _workflow_notes(step_count: int, any_failed: bool, details: list[str]) -> str:
    """`notes` — unchanged in shape, with the raw missing-input diagnostics
    appended. This is the string the UI shows behind the message's "details"
    disclosure and the one that gets logged, so it stays technical; the
    plain-English version travels separately in `failure_message`."""
    note = (
        f"Workflow: {step_count} step(s) ({step_count - 1} + synthesis)"
        f"{' (some steps failed — see workflow_steps)' if any_failed else ''}"
    )
    if details:
        note = f"{note} [{'; '.join(details)}]"
    return note


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
    deliverables: int = 0,
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
    plan = _plan_workflow(req.question, client, overrides, cap, deliverables)
    if plan is None:
        return _single_shot_fallback(req, owner, fallback_category)

    steps = plan["steps"]
    synthesis_instruction = (
        plan["synthesis_instruction"] or _DEFAULT_SYNTHESIS_INSTRUCTION
    )
    total_calls = len(steps) + 1  # +1 for synthesis

    worst_model = _worst_case_model(overrides, steps)
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
            answer="", mode_used=_mode_tag(len(steps) + 1, auto_routed), notes=refusal
        )

    step_records: list[WorkflowStep] = []
    context: list[str] = []
    any_failed = False
    artefacts = _ArtefactBag()
    expected: set[str] = set()
    missing_input_details: list[str] = []

    for index, step in enumerate(steps):
        resolved = _resolve_step_inputs(step, artefacts.produced, expected)
        expected |= _expected_output_names(step)
        if resolved.missing or resolved.unreadable:
            # Stop the step outright, before any model call. Silent
            # regeneration is the worse outcome by a distance: it costs a
            # model call to produce an artefact that looks right and
            # contradicts the one beside it, with nothing anywhere reporting a
            # problem. The remaining steps still run (partial results are
            # preserved), and the synthesis is told not to fill the gap in.
            detail = _missing_input_detail(index, step, resolved, artefacts.produced)
            logger.warning("workflow.step_input_missing %s", detail)
            missing_input_details.append(detail)
            any_failed = True
            step_records.append(_failed_step_record(step))
            context.append(
                _context_block(
                    index, step["category"], step["instruction"], detail, True
                )
            )
            continue

        step_req = AskRequest(
            question=_step_prompt(
                req.question,
                step["instruction"],
                index,
                len(steps),
                context,
                artefact=step["artefact"] if step["produces_artefact"] else "",
                inputs=resolved.available,
            ),
            mode=Mode.auto,
            no_cache=True,
        )
        result = run_orchestrator(
            step_req,
            owner=owner,
            forced_category=step["category"],
            require_code_execution=step["produces_artefact"],
        )
        artefacts.absorb(result)
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
        question=_synthesis_prompt(
            req.question,
            synthesis_instruction,
            context,
            artefacts.describe(),
            skipped=missing_input_details,
        ),
        mode=Mode.auto,
        no_cache=True,
    )
    synthesis_result = run_orchestrator(
        synthesis_req, owner=owner, forced_category=_SYNTHESIS_CATEGORY
    )
    artefacts.absorb(synthesis_result)
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

    return AskResponse(
        answer=answer,
        mode_used=_mode_tag(len(steps) + 1, auto_routed),
        notes=_workflow_notes(len(steps) + 1, any_failed, missing_input_details),
        # Plain English for the user, alongside a real answer: the workflow
        # DID deliver the steps that worked, so this is not the empty-answer
        # failure `failure_message` was introduced for — but a step that was
        # deliberately stopped has to be said out loud somewhere the user
        # actually reads, not left as a "· failed" marker in a collapsed
        # breakdown. Raw detail stays in `notes`, per 8bfc2b8.
        failure_message=(
            _missing_input_failure_message(missing_input_details)
            if missing_input_details
            else None
        ),
        model=synthesis_result.model,
        input_tokens=total_input,
        output_tokens=total_output,
        cost_usd=total_cost,
        workflow_steps=step_records,
        # Files/images the STEPS produced, carried onto the final message so
        # they render exactly as a single-shot answer's do (attachment chip,
        # inline .xlsx/.csv preview, inline image, "Ran code" transparency
        # card). The synthesis step's own artefacts are folded in first.
        code_results=artefacts.as_models(),
        images=artefacts.images or None,
    )


def stream_workflow(
    req: AskRequest,
    owner: str | None = None,
    auto_routed: bool = False,
    fallback_category: str | None = None,
    deliverables: int = 0,
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
    plan = _plan_workflow(req.question, client, overrides, cap, deliverables)
    if plan is None:
        yield from _single_shot_fallback_stream(req, owner, fallback_category)
        return

    steps = plan["steps"]
    synthesis_instruction = (
        plan["synthesis_instruction"] or _DEFAULT_SYNTHESIS_INSTRUCTION
    )
    total_calls = len(steps) + 1
    mode_used = _mode_tag(len(steps) + 1, auto_routed)

    worst_model = _worst_case_model(overrides, steps)
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
    artefacts = _ArtefactBag()
    expected: set[str] = set()
    missing_input_details: list[str] = []

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
            resolved = _resolve_step_inputs(step, artefacts.produced, expected)
            expected |= _expected_output_names(step)
            if resolved.missing or resolved.unreadable:
                # See run_workflow's identical branch: stopped before any
                # model call, remaining steps still run, synthesis told not to
                # improvise a replacement.
                detail = _missing_input_detail(
                    index, step, resolved, artefacts.produced
                )
                logger.warning("workflow.step_input_missing %s", detail)
                missing_input_details.append(detail)
                any_failed = True
                step_records.append(_failed_step_record(step))
                context.append(
                    _context_block(
                        index, step["category"], step["instruction"], detail, True
                    )
                )
                yield {
                    "event": "step",
                    "data": {
                        "index": index,
                        "total": total_calls,
                        "category": step["category"],
                        "instruction": step["instruction"],
                        "status": "failed",
                        "model": None,
                    },
                }
                continue

            step_req = AskRequest(
                question=_step_prompt(
                    req.question,
                    step["instruction"],
                    index,
                    len(steps),
                    context,
                    artefact=step["artefact"] if step["produces_artefact"] else "",
                    inputs=resolved.available,
                ),
                mode=Mode.auto,
                no_cache=True,
            )
            result = run_orchestrator(
                step_req,
                owner=owner,
                forced_category=step["category"],
                require_code_execution=step["produces_artefact"],
            )
            artefacts.absorb(result)
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
            question=_synthesis_prompt(
                req.question,
                synthesis_instruction,
                context,
                artefacts.describe(),
                skipped=missing_input_details,
            ),
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

        yield {
            "event": "done",
            "data": {
                "answer": answer_final,
                "mode_used": mode_used,
                "notes": _workflow_notes(
                    len(steps) + 1, any_failed, missing_input_details
                ),
                # See run_workflow's identical field: plain English for the
                # user, raw detail left in `notes`. The client already reads
                # `failure_message` off the "done" event as the headline.
                "failure_message": (
                    _missing_input_failure_message(missing_input_details)
                    if missing_input_details
                    else None
                ),
                "model": synthesis_model,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cost_usd": total_cost,
                "workflow_steps": [s.model_dump() for s in step_records],
                # See run_workflow's identical fields: step artefacts ride the
                # final message so they render like any single-shot answer's.
                "code_results": artefacts.code_results or None,
                "images": artefacts.images or None,
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
