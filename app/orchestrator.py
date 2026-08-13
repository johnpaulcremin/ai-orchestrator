"""Route + answer entry points (run_orchestrator, stream_orchestrator) and the
small amount of glue only they need: the research-mode override and concise-
mode prompt injection. Everything else this module used to contain — response
extraction, spend logging, response/semantic cache plumbing, summarization,
optional-tool building, and the provider-dispatch call chain — now lives in
the sibling app/orchestrator_*.py modules and is re-imported here for
whichever names run_orchestrator/stream_orchestrator's own bodies (or other
modules importing from here) still reference by bare name.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Generator
from typing import Any

from dotenv import load_dotenv

from . import budget, cache, database, free_tier, rag_library, semantic_cache  # noqa: F401 (database re-exported for orchestrator_spend/tests)
from .academic_search import (
    academic_search_enabled,
    format_note as academic_search_note,
    looks_like_academic_search_request,
    search_papers,
)
from .actions import actions_enabled
from .fact_check import (
    check_claim,
    fact_check_enabled,
    format_note as fact_check_note,
    looks_like_fact_check_request,
)
from .self_describe import (
    capabilities_snapshot,
    format_note as self_describe_note,
    grounded_question as self_describe_grounded_question,
    looks_like_capabilities_request,
    looks_like_improvement_request as self_describe_improvement_request,
    self_describe_enabled,
)
from .image_processing import process_images
from .moderation import check_question, moderation_enabled, refusal_note
from .observability import enrich_span
from .orchestrator_cache import (
    _cache_key,
    _cacheable_shape,
    _cached_hit_note,
    _cached_response,
    _semantic_cached_hit_note,
    _semantic_cached_response,
)
from .orchestrator_calls import (  # noqa: F401 (some re-exported for other modules/tests)
    _auth_key_env,
    _build_input,
    _call_model,
    _fallback_models,
    _stream_model,
    _vendor_of,
    get_client,
)
from .orchestrator_extract import (  # noqa: F401 (some re-exported for other modules/tests)
    Citation,
    CodeResultDict,
    PendingActionDict,
    _code_execution_note,
    SELF_DESCRIBE_NOTE_FAILED,
    TRUNCATED_EMPTY_ANSWER,
    _compose_answer_with_notes,
    _extract_citations,
    _extract_code_results,
    _extract_images,
    _extract_pending_action,
    _extract_search_queries,
    _extract_text,
    _image_generation_failed_note,
    _image_generation_note,
    _record_openai_usage,
    _usage_fields,
    _MAX_CITATIONS,
    _MAX_SEARCH_QUERIES,
)
from .file_claims import claims_unproduced_file
from .file_claims import format_note as file_claim_note
from .image_claims import claims_unproduced_image
from .image_claims import format_note as image_claim_note
from .database import deployment_id as db_deployment_id
from .fallback_reason import (
    BUDGET_REFUSAL,
    REASON_LABELS,
    classify_error_reason,
)
from .orchestrator_spend import (
    _record_avoided_cost,
    _record_fallback_event,
    _record_free_tier_avoided_cost,
    _record_spend,
)
from .orchestrator_summarize import (  # noqa: F401 (re-exported: see app/routers/messages.py, app/routers/conversations.py)
    summarize_conversation_for_display,
    summarize_text,
)
from .orchestrator_tools import (  # noqa: F401 (some re-exported for other modules/tests)
    _build_action_tool,
    _build_image_generation_tool,
    _build_tools,
    _code_execution_enabled,
    _image_generation_enabled,
    _image_generation_model,
    _image_generation_provider,
    _image_generation_quality,
    _image_generation_size,
    _looks_like_artefact_request,
    _looks_like_image_request,
    prefers_drawn_by_code,
    artefact_file_instructions,
    _math_solve_enabled,
    _worst_case_image_cost,
)
from .providers import (
    AUTH_ERRORS,
    RATE_ERRORS,
    TIMEOUT_ERRORS,
    generate_images_litellm,
    provider_of,
)
from .routing import (
    _WEB_SEARCH_PROVIDERS,
    RouteDecision,
    auto_workflow_enabled,
    decide_route,
)
from .schemas import (
    AcademicResult,
    AskRequest,
    AskResponse,
    CodeResult,
    FactCheck,
    LibrarySource,
    MathResult,
    MemorySource,
    PendingAction,
    Source,
)
from .categories import CATEGORY_PROMPT_DEFAULTS, retrieval_helps
from .context_fencing import fence_reference
from .settings import (
    bool_setting,
    category_prompt_key,
    get_model_overrides,
    model_setting,
)
from .telemetry import StageTimer, elapsed_ms, logger, new_request_meta
from .usage import (
    Usage,
    estimate_code_execution_cost,
    estimate_cost,
    estimate_image_cost,
)

load_dotenv()

# Providers with a hosted/native propose_action tool wired up (see
# providers.call_anthropic's _anthropic_action_tool and
# orchestrator_tools._build_action_tool) — a Gemini/Bedrock/Mistral/other
# LiteLLM-routed model never gets it, same reasoning as _WEB_SEARCH_PROVIDERS.
_ACTION_PROVIDERS = {"openai", "anthropic"}

# Same reasoning, for the hosted code-execution tool (OpenAI's
# code_interpreter, Anthropic's beta code_execution — see
# providers.call_anthropic's _ANTHROPIC_CODE_EXECUTION_TOOL and
# orchestrator_tools._CODE_INTERPRETER_TOOL).
_CODE_EXECUTION_PROVIDERS = {"openai", "anthropic"}

# Same reasoning, for the math_solve function/custom tool (see
# providers._anthropic_math_solve_tool and
# orchestrator_tools._build_math_solve_tool).
_MATH_SOLVE_PROVIDERS = {"openai", "anthropic"}

# Same reasoning, for the app_capabilities function/custom tool (see
# providers._anthropic_self_describe_tool and
# orchestrator_tools._build_self_describe_tool). A provider outside this set
# (every LiteLLM-routed model) falls back to the phrase heuristic instead
# (see _tool_flags_for's self_describe_heuristic_wanted) — same
# "heuristic fallback for a provider with no native tool" split as image
# generation's images_wanted (the OpenAI-hosted tool) vs
# standalone_image_wanted (phrase heuristic + a direct image call).
_SELF_DESCRIBE_TOOL_PROVIDERS = {"openai", "anthropic"}


def _should_auto_workflow(
    decision: RouteDecision,
    allow_auto_workflow: bool,
    forced_category: str | None,
) -> bool:
    """Whether to hand this request to workflow mode instead of answering it
    single-shot.

    Four conditions, all required, and every one of them is a brake rather
    than an accelerator — the single-shot path already handles the
    overwhelming majority of questions well, so this only fires when there is
    real evidence it shouldn't:

    * the classifier said several distinct artefacts (already cross-checked
      against `deliverables >= 2` in routing._parse_classifier_json);
    * the operator opted in (AUTO_WORKFLOW, off by default);
    * `allow_auto_workflow` — cleared by workflow.py's own fallback into the
      orchestrator, without which a failed plan would bounce straight back
      into a new workflow, forever;
    * `forced_category is None` — a workflow STEP is already one slice of a
      plan and must never spawn a nested workflow of its own. Redundant with
      the classification routing.decide_route hard-codes for a forced
      category, and kept anyway: this is the loop-bearing path, so it is
      worth two independent guards.

    `decision.ambiguous` needs no check here — the router returns a
    `auto->clarify` decision with multi_part left at its False default, so an
    ambiguous request can never reach this.
    """
    return (
        decision.multi_part
        and allow_auto_workflow
        and forced_category is None
        and auto_workflow_enabled()
    )


def _apply_code_execution_override(
    decision: RouteDecision, require_code_execution: bool
) -> RouteDecision:
    """Make sure a step whose whole job is to PRODUCE A FILE lands on a model
    that can actually run code.

    Category routing alone is not enough, and that is the bug this exists for:
    a workflow step tagged `summarization` or `simple_transform` resolves to
    the fast/budget tier, which on this app can legitimately be a
    Gemini/Ollama/LiteLLM model — and code execution is gated on
    `provider_of(model) in _CODE_EXECUTION_PROVIDERS`, so the tool would be
    silently absent and the step would write a markdown table instead of
    producing the .xlsx it was asked for.

    It ALSO raises the output ceiling, and that half exists for a failure just
    as concrete. A step is capped by its category's tier
    (routing.tier_output_caps — 800 budget / 1500 fast / 4000 smart), and a
    file-producing step does not merely describe its data, it emits CODE that
    embeds the data. Observed live: an artefact step tagged `summarization`
    ran on the fast tier's 1500 tokens, was asked for items 14-25, and wrote a
    spreadsheet containing 14-19 with the last row missing a field. A text
    ceiling on a file-sized job truncates the deliverable mid-structure, and
    the result looks complete.

    So the ceiling is raised to artefact_max_output_tokens(), and only ever
    raised: max() with whatever the tier already allowed, so a smart-tier
    artefact step never has its budget cut to fit this number. Reasoning
    effort is still untouched, and prose steps are untouched entirely — their
    category routing works and stays exactly as it is.

    A no-op when CODE_EXECUTION is off, or when nothing configured can run
    code: the file cannot be produced either way, so the step degrades to
    text and a bigger text budget would just cost more for the same answer.
    """
    if not require_code_execution or not _code_execution_enabled():
        return decision
    capable = code_execution_capable_model(decision.model)
    if capable is None:
        # Nothing configured can run code — degrade to text rather than fail.
        logger.warning(
            "workflow.artefact_step_no_capable_model model=%s", decision.model
        )
        return decision
    # Deliberately independent of whether the MODEL changed: a step already on
    # a capable tier still has a text-sized ceiling, and that is what cut the
    # observed spreadsheet short.
    ceiling = max(decision.max_output_tokens, artefact_max_output_tokens())
    if capable == decision.model and ceiling == decision.max_output_tokens:
        return decision
    note = f"{decision.notes}"
    if capable != decision.model:
        note = (
            f"{note} | artefact: moved from {decision.model} to "
            f"{capable} for code execution"
        )
    if ceiling != decision.max_output_tokens:
        note = (
            f"{note} | artefact: output ceiling raised from "
            f"{decision.max_output_tokens} to {ceiling}"
        )
    return dataclasses.replace(
        decision, model=capable, max_output_tokens=ceiling, notes=note
    )


def artefact_max_output_tokens() -> int:
    """Output ceiling for a step that must PRODUCE A FILE.

    Its own setting rather than a tier's, because it is sizing a different
    kind of output: a tier cap sizes prose a person will read, while an
    artefact step emits code carrying every row of the data. The default is
    deliberately well above the smart tier's — a spreadsheet of a few dozen
    rich rows does not fit in 4000 tokens, and the failure mode is a file
    that stops mid-structure while looking finished.

    Only ever RAISES a step's ceiling (see _apply_code_execution_override's
    max()), so lowering this cannot shrink a tier that already allows more.
    """
    raw = (os.getenv("ARTEFACT_MAX_OUTPUT_TOKENS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 8000
    return value if value > 0 else 8000


@dataclasses.dataclass
class _FallbackTools:
    """Every hosted tool re-derived for the model a failover is ABOUT to call,
    with the collectors its results land in.

    The fallback used to dispatch with no tools at all — a documented scope
    limit, on the reasoning that a fallback provider might not support the
    primary's. That reasoning was sound about the PRIMARY's flags and wrong as
    a conclusion: the answer is to ask what THIS model supports, not to give
    it nothing. A freshness question that failed over came back ungrounded, an
    image request came back imageless, and a fact-check or academic lookup —
    which are standalone HTTP calls that never touched the model at all — was
    skipped for no reason beyond sharing the code path.

    Exists as one object because run_orchestrator and stream_orchestrator each
    carry their own copy of the failover loop. Nine flags and nine collectors
    mirrored by hand in two places is a drift waiting to happen; this way both
    loops ask the same function the same question.
    """

    web_search: bool
    actions: bool
    images: bool
    standalone_image: bool
    code_execution: bool
    math_solve: bool
    capabilities: bool
    fact_check: bool
    academic_search: bool
    self_describe_heuristic: bool
    citations: list[Citation] = dataclasses.field(default_factory=list)
    search_queries: list[str] = dataclasses.field(default_factory=list)
    pending_action: list[PendingActionDict] = dataclasses.field(default_factory=list)
    generated_images: list[str] = dataclasses.field(default_factory=list)
    code_results: list[CodeResultDict] = dataclasses.field(default_factory=list)
    math_results: list[dict[str, object]] = dataclasses.field(default_factory=list)
    capabilities_calls: list[bool] = dataclasses.field(default_factory=list)
    fact_checks: list[dict[str, object]] = dataclasses.field(default_factory=list)
    academic_results: list[dict[str, object]] = dataclasses.field(default_factory=list)

    def cacheable(self, needs_live_data: bool) -> bool:
        """Whether this fallback's answer may be frozen into either cache.

        The same list the primary path applies, read off THIS call's own
        collectors — which is the bug the old check had even before tools
        arrived: it consulted the PRIMARY's `pending_action`/`generated_images`
        lists, which belong to a call that had already failed.

        `needs_live_data` still excludes on its own, exactly as on the primary
        path and for the same reason: a freshness-sensitive answer goes stale
        in a cache whether or not a search grounded it.
        """
        return not (
            needs_live_data
            or self.pending_action
            or self.generated_images
            or self.code_results
            or self.fact_checks
            or self.academic_results
            or self.math_results
            or self.capabilities_calls
            or self.self_describe_heuristic
        )


def _fallback_tools(
    model: str, req: AskRequest, needs_live_data: bool
) -> _FallbackTools:
    """Ask _tool_flags_for about the FALLBACK model, so the answer describes
    what it can actually do rather than what the primary could."""
    (
        actions_wanted,
        images_wanted,
        standalone_image_wanted,
        code_execution_wanted,
        math_solve_wanted,
        fact_check_wanted,
        academic_search_wanted,
        self_describe_tool_wanted,
        self_describe_heuristic_wanted,
        _,
    ) = _tool_flags_for(model, req, needs_live_data)
    return _FallbackTools(
        # Not model-derived, and deliberately so: web_search rides the routing
        # decision (routing gates it on the flag and the provider before it
        # ever gets here), and a provider with no hosted-tool support ignores
        # it — see _call_model's docstring on LiteLLM.
        web_search=needs_live_data,
        actions=actions_wanted,
        images=images_wanted,
        standalone_image=standalone_image_wanted,
        code_execution=code_execution_wanted,
        math_solve=math_solve_wanted,
        capabilities=self_describe_tool_wanted,
        fact_check=fact_check_wanted,
        academic_search=academic_search_wanted,
        self_describe_heuristic=self_describe_heuristic_wanted,
    )


def code_execution_available_to(model: str) -> bool:
    """Whether `model` would actually be OFFERED the code-execution tool.

    Pulled out of _tool_flags_for so the FALLBACK path can ask the same
    question about the model it is about to call. That path deliberately
    dispatches without web_search/actions/images (a documented scope limit —
    a fallback provider may not support them at all), but code execution is
    different: a request whose whole point is a FILE gets nothing useful from
    a tool-less retry, and the answer then has to explain an absent
    deliverable. So the fallback re-derives this one flag for ITS OWN model
    rather than inheriting anything, which is why it is a function of the
    model alone.

    workflow._no_artefact_reason asks it too, about the model that actually
    answered — a failover can still land somewhere incapable (a Gemini fast
    tier, say), and "the step returned text instead" would then be the wrong
    diagnosis for a step that was never given the tool.
    """
    return _code_execution_enabled() and provider_of(model) in _CODE_EXECUTION_PROVIDERS


def code_execution_capable_model(current: str) -> str | None:
    """The model an artefact step would ACTUALLY run on, given `current` as
    its category's choice — `current` itself when that can already run code,
    otherwise the first configured tier that can, or None if nothing can.

    Shared deliberately: `_apply_code_execution_override` uses it to pick the
    model, and workflow._worst_case_model uses it to price the up-front
    reservation. If these two ever disagreed, the reservation would be
    quoting a model the workflow does not use.
    """
    if provider_of(current) in _CODE_EXECUTION_PROVIDERS:
        return current
    overrides = get_model_overrides()
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    for candidate in (
        model_setting("OPENAI_MODEL_SMART", base, overrides),
        base,
        model_setting("OPENAI_MODEL_FAST", base, overrides),
    ):
        if candidate and provider_of(candidate) in _CODE_EXECUTION_PROVIDERS:
            return candidate
    return None


def _apply_research_override(decision: RouteDecision, req: AskRequest) -> RouteDecision:
    """Research mode: force web_search on for this one request, regardless of
    the classifier's freshness judgment — for "look this up properly" asks
    the auto-mode heuristic might not flag as needing live data.

    Subject to the same gating _gate_live_data already applies: WEB_SEARCH
    must be enabled AND the resolved model must be served by a provider with
    a hosted web-search tool wired up (OpenAI or Anthropic). Forcing it
    otherwise would just set a flag nothing downstream acts on.

    A denied override SAYS SO in the notes rather than passing silently. The
    composer's globe button is a direct instruction — "force a live web
    search for this question" — and an instruction that no-ops without a word
    is the same defect as an image request routed to a model that cannot make
    one: the user is left to conclude the app has no internet access at all,
    and the model, which is never told the search was withheld, will happily
    agree with them. Cheap to say, and it lands in the same `details` line
    that already reports the routing decision.
    """
    if not req.research or decision.needs_live_data:
        return decision
    if not bool_setting("WEB_SEARCH", False):
        return dataclasses.replace(
            decision,
            notes=(
                f"{decision.notes} | research mode requested but WEB_SEARCH "
                "is off (Settings > Web search retrieval)"
            ),
        )
    if provider_of(decision.model) not in _WEB_SEARCH_PROVIDERS:
        return dataclasses.replace(
            decision,
            notes=(
                f"{decision.notes} | research mode requested but "
                f"{decision.model} has no hosted web search "
                "(OpenAI/Anthropic models only)"
            ),
        )
    return dataclasses.replace(
        decision,
        needs_live_data=True,
        notes=f"{decision.notes} | research mode: forced web search",
    )


def _free_lane_smart_enabled() -> bool:
    return bool_setting("FREE_LANE_SMART", False)


def _self_describe_grounded_answer(
    decision: RouteDecision,
    question: str,
    note: str,
    usage: Usage,
    cacheable_system: str | None,
) -> str:
    """One extra call that answers `question` with the capability facts in
    hand, for a turn where the model replied with the app_capabilities tool
    call and no prose of its own.

    Returns "" if it produces nothing, leaving the caller to fall back to the
    note-only answer — never worse than the old behaviour.

    EVERY tool is off on this call, `capabilities` included: the facts are
    already in the prompt, so offering the tool again would just produce a
    second textless turn and, with it, an unbounded loop. `usage` is the
    caller's own accumulator, so this call's tokens land in the same answer's
    reported cost rather than going unbilled — it is a second paid call and
    the app says so (see the `| grounded self-describe` note the caller
    appends).
    """
    return _call_model(
        model=decision.model,
        question=self_describe_grounded_question(question, note),
        max_output_tokens=decision.max_output_tokens,
        reasoning_effort=decision.reasoning_effort,
        usage=usage,
        cacheable_system=cacheable_system,
        # Deliberately NOT anthropic_question: that is the cache-split variant
        # of the ORIGINAL question, and would silently discard the facts.
    ).strip()


def _tool_flags_for(
    model: str, req: AskRequest, needs_live_data: bool
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool, bool, bool]:
    """(actions_wanted, images_wanted, standalone_image_wanted,
    code_execution_wanted, math_solve_wanted, fact_check_wanted,
    academic_search_wanted, self_describe_tool_wanted,
    self_describe_heuristic_wanted, any_wanted) for `model` answering `req`
    — the same per-tool eligibility checks run_orchestrator/stream_orchestrator
    each already computed inline before dispatch, pulled into one shared
    helper so it can ALSO be evaluated against the pre-free-tier "paid"
    decision to gate free-tier eligibility (see _apply_free_tier_override's
    `tools_wanted` param docstring): a free-tier model can't be assumed to
    support the same provider-hosted tools the paid model would have, so a
    turn that wants any of them must never be silently downgraded to one.

    self_describe_tool_wanted (the app_capabilities tool is OFFERED — the
    model itself decides whether to call it, same as math_solve_wanted) and
    self_describe_heuristic_wanted (a LiteLLM-routed model with no native
    tool-calling wired up here falls back to the phrase heuristic instead —
    see looks_like_capabilities_request) are mutually exclusive by
    construction: they're gated on disjoint provider sets.
    """
    provider = provider_of(model)
    actions_wanted = actions_enabled() and provider in _ACTION_PROVIDERS
    # The hosted image_generation tool exists only on OpenAI's Responses API,
    # so it can only be OFFERED when an OpenAI model is the one answering.
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider == "openai"
    )
    # Everything else that wants an image goes through the standalone call.
    # That covers two cases, not one: the Gemini backend (which has no tool a
    # chat model can call, so it was always dispatched this way), AND the
    # OpenAI backend when the router picked a non-OpenAI model to answer —
    # which the tool gate above cannot serve. Before this, the second case
    # produced no image at all: "draw me an image of X" routed to the smart
    # tier (Claude) or the budget tier (Ollama) silently came back as text,
    # and the answer, grounded only on the docs, improvised an explanation
    # about phrase heuristics for a call that was never reachable.
    code_execution_wanted = code_execution_available_to(model)
    # ...except for a DIAGRAM, where code execution is the better instrument
    # and is already here. Observed: asked for a diagram of this app, Claude
    # wrote SVG programmatically and delivered a real hub-and-spoke drawing
    # with legible labels — an image model asked the same thing returns an
    # artistic impression with garbled text, for $0.19. The exclusion of
    # chart/graph/plot from the picture-noun list was this same judgement,
    # made one noun short: a diagram is a drawing of a STRUCTURE, and
    # structure survives being drawn by code in a way it does not survive
    # being imagined. This also closes the hole that exclusion left open —
    # "draw me a chart" still reached the image path through the VERB rule.
    #
    # Conditional on code execution actually being available to the answering
    # model: where it is not, an image model is the only instrument there is,
    # and a mediocre diagram beats none.
    standalone_image_wanted = (
        _image_generation_enabled()
        and not images_wanted
        and _looks_like_image_request(req.question)
        and not (code_execution_wanted and prefers_drawn_by_code(req.question))
    )
    math_solve_wanted = _math_solve_enabled() and provider in _MATH_SOLVE_PROVIDERS
    fact_check_wanted = fact_check_enabled() and looks_like_fact_check_request(
        req.question
    )
    academic_search_wanted = (
        academic_search_enabled() and looks_like_academic_search_request(req.question)
    )
    self_describe_tool_wanted = (
        self_describe_enabled() and provider in _SELF_DESCRIBE_TOOL_PROVIDERS
    )
    self_describe_heuristic_wanted = (
        self_describe_enabled()
        and provider not in _SELF_DESCRIBE_TOOL_PROVIDERS
        and (
            looks_like_capabilities_request(req.question)
            # A self-critique question ("what are your weaknesses?") is a
            # question about the app, but matches none of the capabilities
            # phrases — so without this a LiteLLM-routed model (Gemini,
            # Ollama, the budget lane) answered the one question this
            # grounding exists for with no grounding at all.
            or self_describe_improvement_request(req.question)
        )
    )
    any_wanted = (
        needs_live_data
        or actions_wanted
        or images_wanted
        or standalone_image_wanted
        or code_execution_wanted
        or math_solve_wanted
        or fact_check_wanted
        or academic_search_wanted
        or self_describe_tool_wanted
        or self_describe_heuristic_wanted
    )
    return (
        actions_wanted,
        images_wanted,
        standalone_image_wanted,
        code_execution_wanted,
        math_solve_wanted,
        fact_check_wanted,
        academic_search_wanted,
        self_describe_tool_wanted,
        self_describe_heuristic_wanted,
        any_wanted,
    )


def _live_tool_names(model: str, req: AskRequest, needs_live_data: bool) -> list[str]:
    """Plain-English names of the optional tools `model` ACTUALLY has on this
    turn — the per-turn truth the self-describe note reports alongside the
    owner's flag list, which is only ever a statement about configuration.

    Recomputed from _tool_flags_for rather than threaded down from the caller
    so the four note sites (ask/stream, each with a fallback twin) can each
    ask about whichever model is really answering there. It is a pure
    settings read — no I/O, no tokens.
    """
    (
        actions,
        images,
        standalone_image,
        code_execution,
        math_solve,
        fact_check,
        academic_search,
        self_describe_tool,
        _,
        _,
    ) = _tool_flags_for(model, req, needs_live_data)
    names: list[str] = []
    if needs_live_data:
        names.append("live web search")
    if images:
        names.append("image generation (hosted tool you call yourself)")
    elif standalone_image:
        names.append("image generation (fires automatically for this question)")
    if code_execution:
        names.append("code execution")
    if math_solve:
        names.append("precision math (SymPy)")
    if actions:
        names.append("propose_action (webhooks)")
    if fact_check:
        names.append("fact-check lookup")
    if academic_search:
        names.append("academic search")
    if self_describe_tool:
        names.append("app_capabilities")
    return names


def _self_describe_note_safely(
    owner: str | None,
    model: str,
    req: AskRequest,
    needs_live_data: bool,
    include_subsystems: bool = False,
) -> str:
    """The self-describe note for `model`, or "" if composing it fails.

    NEVER RAISES — the convention its sibling enrichments state outright
    ("Never raises: this is an enrichment, not worth failing the answer
    over" — fact_check.check_claim, academic_search.search_papers) and the
    one place in the answer path that did not follow it. It is also the
    heaviest of them: capabilities_snapshot() reads the spend and free-lane
    tables AND parses the source tree (see app/codebase_inventory.py), and
    app/self_describe.py contains no exception handler anywhere.

    It runs inside the same try that wraps the model call, so anything it
    raised was caught by `except Exception as primary_error` and read as
    "the primary model failed": an answer already generated and PAID FOR
    was discarded, and the question went down the fallback chain to be paid
    for a second time. Every other post-answer step here — cache.put,
    semantic_cache.put, _record_spend — already guards itself for exactly
    this reason. Seen once for real, from a stale test stub whose signature
    no longer matched; a locked database in production has the same shape.
    """
    try:
        return self_describe_note(
            capabilities_snapshot(owner),
            include_subsystems=include_subsystems,
            answering_model=model,
            live_tools=_live_tool_names(model, req, needs_live_data),
        )
    except Exception:
        logger.exception("self_describe.note_failed model=%s", model)
        return ""


def _apply_free_tier_override(
    decision: RouteDecision, tools_wanted: bool
) -> RouteDecision:
    """Substitute a $0 provider free-tier model (see app/free_tier.py) for
    the resolved model, when eligible — BEFORE this call would otherwise
    dispatch to a real paid budget/fast-tier model.

    Auto-mode traffic only: an explicit (non-auto) fast/budget/smart mode
    means the caller deliberately chose that tier, and a forced/switch-model
    decision (mode_used starts with "forced:") means they chose an exact
    model — neither gets silently swapped for a free one. A configured
    per-category model override (mode_used containing ":", e.g.
    "auto->fast:coding") is excluded for the same reason: the operator
    explicitly chose that model for this category. Smart-tier auto results
    are excluded unless FREE_LANE_SMART is on (see its own docstring) — a
    free-tier model is typically a small/cheap one, and silently downgrading
    a smart-tier answer's quality needs an explicit opt-in. `tools_wanted`
    (see _tool_flags_for, evaluated against the PAID decision before this
    call) excludes any turn that would use a provider-hosted tool this turn,
    since a free-tier model can't be assumed to support it. An ambiguous
    decision (no model call happens at all) needs no substitution either.

    Records one unit of quota usage against the chosen model right here, at
    decision time — not after the call succeeds — the same "reserve the
    worst case up front" philosophy budget.reserve already uses, so a
    request that gets substituted and then fails downstream still counts
    against today's tracked quota rather than being retried against the
    same exhausted-looking allowance. A dispatch failure additionally
    cools the model down for the rest of the day (see
    free_tier.exhaust_for_today, applied in the fallback loop below) rather
    than just leaving today's single recorded unit in place.
    """
    if not free_tier.enabled() or decision.ambiguous or tools_wanted:
        return decision
    if decision.mode_used.startswith("forced:") or not decision.mode_used.startswith(
        "auto->"
    ):
        return decision
    tier_and_category = decision.mode_used[len("auto->") :]
    if ":" in tier_and_category:
        return decision
    if tier_and_category == "smart" and not _free_lane_smart_enabled():
        return decision
    model = free_tier.pick_available_model()
    if model is None or model == decision.model:
        return decision
    free_tier.record_use(model)
    return dataclasses.replace(
        decision,
        model=model,
        mode_used=f"auto->free:{model}",
        notes=f"{decision.notes} | free-tier: routed to {model} (quota remaining today)",
    )


# Plain-English failure messages: what actually gets surfaced as the ANSWER
# (AskResponse.failure_message / the stream "error" event's "message") when a
# request fails outright — never what's RECORDED. The existing raw diagnostic
# (exception type/text, request_id, elapsed ms) keeps flowing into `notes`
# completely unchanged in every one of these branches; it's still what's
# logged server-side and still what a client can show in a details
# disclosure. This is purely a second, human-readable string alongside it,
# for the headline a user actually reads. Two of the four failure kinds this
# covers (timeout, provider error/rate-limit) are decided by exception TYPE
# here, in Python, using the real exception object — not by pattern-matching
# the rendered notes string later, which would be both fragile and unable to
# reuse the same real ms/exception-type values notes already computed from.
# The other two kinds (budget refusal, cancelled) are handled at their own
# call sites: budget's refusal text is already plain English (see
# budget.reserve), reused as-is; "cancelled" never reaches the backend at
# all (an aborted fetch just closes the connection — see stream_orchestrator's
# GeneratorExit branch, and frontend/src/App.tsx's own "Stopped." status for
# the client-side equivalent), so there's no backend message to generate for
# it here.
def _timeout_failure_message(ms: int) -> str:
    seconds = max(1, round(ms / 1000))
    return (
        f"That request timed out after ~{seconds}s — it was likely too large "
        "to complete in one pass. Try asking for one part at a time, or "
        "regenerate."
    )


def _provider_error_failure_message() -> str:
    return (
        "That request failed due to a provider error, not something in your "
        "question. Try regenerating — if it keeps happening, try a "
        "different model or tier."
    )


def _fallback_exhausted_failure_message(primary_error: BaseException, ms: int) -> str:
    """The plain-English counterpart to the "Primary model failed and no "
    fallback succeeded"/"Rate limited..." notes text built at each of this
    function's two call sites (run_orchestrator, stream_orchestrator) — same
    timeout-vs-generic distinction, by the primary error's actual type."""
    if isinstance(primary_error, TIMEOUT_ERRORS):
        return _timeout_failure_message(ms)
    return _provider_error_failure_message()


# Output tokens typically bill 3-10x the input rate, so a verbose answer costs
# far more than a verbose question — this is the one lever here that targets
# OUTPUT length rather than input/context size. Off by default (unlike
# IMAGE_DOWNSCALE/OCR_REPLACEMENT): it changes what the model actually says,
# not just what it's billed for, so it needs an explicit opt-in rather than
# defaulting on.
_CONCISE_INSTRUCTION = (
    "Be concise: answer directly with no preamble, no restating the question, "
    "and no filler or hedging. Prefer short paragraphs, bullet points, or "
    "terse code over verbose prose — but never omit necessary detail or "
    "correctness just to be shorter."
)


def _concise_mode_enabled() -> bool:
    return bool_setting("CONCISE_MODE", False)


def apply_concise_mode(
    question: str, cacheable_system: str | None
) -> tuple[str, str | None]:
    """When CONCISE_MODE is on, appends a brevity instruction to the outgoing
    prompt. Threaded into both `question` (what OpenAI/LiteLLM see, and what
    Anthropic sees whenever there's no cacheable_system split) AND
    `cacheable_system` (so Anthropic still gets it when the system-prompt/
    history block is instead sent via the native `system` param — in that
    case `question` itself is never sent, see _call_model's effective_question
    branch, so the instruction has to live in cacheable_system too to reach
    the model at all).
    """
    if not _concise_mode_enabled():
        return question, cacheable_system
    new_question = f"{question}\n\n{_CONCISE_INSTRUCTION}"
    new_cacheable_system = (
        f"{cacheable_system}\n\n{_CONCISE_INSTRUCTION}"
        if cacheable_system is not None
        else cacheable_system
    )
    return new_question, new_cacheable_system


def apply_category_role_prompt(
    category: str, question: str, cacheable_system: str | None
) -> tuple[str, str | None]:
    """When routing resolved a task category (auto mode) AND that category has
    a configured CATEGORY_PROMPT_<category> role prompt (see settings.py's
    category_prompt_key; empty by default for most categories, but see
    categories.CATEGORY_PROMPT_DEFAULTS for the three -- planning, coding,
    analysis -- that ship a built-in "plan before you produce" default, an
    explicit override/env value still wins over it same as any other
    category prompt), PREPENDS it to the outgoing prompt — ahead of
    everything else already in `cacheable_system` (the per-conversation
    custom instructions/history-summary framing context_builder already
    assembled) and, in turn, ahead of apply_concise_mode's instruction
    (called after this one). No-ops when `category` is "" (no classification
    ran — a forced mode/model never gets a role prompt) or the category has
    no configured prompt, so an unconfigured deployment behaves exactly as
    before this feature existed for every category OTHER than the three
    with a built-in default.

    Threaded into both `question` (what OpenAI/LiteLLM see, and what
    Anthropic sees whenever there's no cacheable_system split) AND
    `cacheable_system` (Anthropic's native `system` param), the same dual-
    injection apply_concise_mode uses and for the same reason.

    Deliberately a PREPEND, not an append: the role prompt is constant for a
    given category across every turn, so it belongs at the very front of the
    stable, cacheable prefix -- the one place both Anthropic's cache_control
    checkpointing and OpenAI's implicit prefix caching actually key off of.
    Prepending only ever GROWS that stable prefix, never invalidates it.
    """
    if not category:
        return question, cacheable_system
    prompt = model_setting(
        category_prompt_key(category), CATEGORY_PROMPT_DEFAULTS.get(category, "")
    )
    if not prompt:
        return question, cacheable_system
    new_question = f"{prompt}\n\n{question}"
    new_cacheable_system = (
        f"{prompt}\n\n{cacheable_system}" if cacheable_system else prompt
    )
    return new_question, new_cacheable_system


def _library_block(library_snippets: list[str]) -> str:
    """The recalled document-library chunks as one fenced reference block —
    the same framing app/context_builder.py's _memory_block gives recalled
    cross-conversation memory (see app/context_fencing.py for why both go
    through one fencing helper). "" for no snippets."""
    if not library_snippets:
        return ""
    return fence_reference(
        "Relevant context from your document library (may or may not "
        "actually be relevant here — use your own judgment, and don't "
        "assume the current question is about these documents unless it "
        "clearly is):",
        library_snippets,
    )


def _recall_library_context(
    category: str,
    question: str,
    owner: str | None,
    wanted: bool,
    timer: StageTimer,
) -> tuple[list[str], list[dict[str, Any]]]:
    """(snippets, sources) recalled from the owner's document library for this
    turn, or ([], []) when nothing should be retrieved at all.

    THE GATE. Three things must all hold before an embedding call and a
    library scan are spent:

    - `wanted`: only the ask paths recall the library (see
      run_orchestrator's `recall_library`); regenerate/edit/workflow never do.
    - `retrieval_helps(category)`: the classifier's task category (see
      app/categories.py) is not one that operates purely on the text supplied
      with the request. This is the fix for retrieval CONTAMINATING a
      transform: nothing external can help "rewrite this paragraph, translate
      it, lay it out as a table", but a paragraph that happens to be about a
      topic the library covers will match it, and the answer then drifts into
      explaining the documents. Reads the classification the router already
      made — there is deliberately no second model call here.
    - RAG_LIBRARY is on (checked inside rag_library.recall).

    Called AFTER routing for exactly that reason, which is also why the
    recalled block is applied to an already-assembled prompt by
    apply_library_context below rather than folded in by context_builder.

    `category` is "" whenever no classification ran (explicit fast/smart/
    budget, a forced model, the heuristic fallback) — retrieval_helps returns
    True for it, so those paths behave exactly as they did before the gate.
    """
    if not wanted or not retrieval_helps(category):
        return [], []
    snippets, sources, duration_ms = rag_library.recall(question, owner)
    if rag_library.rag_library_enabled():
        # Only when the feature is actually on: a `library_embed=0ms` entry on
        # every request in a deployment that never enabled it is pure noise.
        timer.record("library_embed", duration_ms)
    return snippets, sources


def apply_library_context(
    library_snippets: list[str], question: str, cacheable_system: str | None
) -> tuple[str, str | None]:
    """Appends the recalled document-library block (_library_block) to the
    outgoing prompt. No-ops when nothing was recalled — which, after
    _recall_library_context's gate, is the normal case for a transform task.

    Threaded into both `question` and `cacheable_system`, the same dual
    injection apply_concise_mode uses and for the same reason (see its
    docstring): whichever of the two a provider path actually sends, the
    block has to be in it.

    APPENDED, not prepended — the opposite of apply_category_role_prompt, and
    for the mirror-image reason. A category role prompt is constant for a
    category, so it belongs at the front where prompt caching keys off. These
    snippets are re-retrieved per turn and differ every time, so they belong
    at the END of the stable prefix, where they invalidate as little of it as
    possible.
    """
    block = _library_block(library_snippets)
    if not block:
        return question, cacheable_system
    new_cacheable_system = (
        f"{cacheable_system}\n\n{block}" if cacheable_system else cacheable_system
    )
    return f"{question}\n\n{block}", new_cacheable_system


def apply_artefact_instructions(
    wants_artefact: bool, question: str, cacheable_system: str | None
) -> tuple[str, str | None]:
    """Tell a plain single ask that its job is to PRODUCE A FILE, in the same
    words a workflow's artefact step is told (see
    orchestrator_tools.artefact_file_instructions).

    Raising the output ceiling was necessary and not sufficient. Verified on
    the live app: with the tool attached, the model code-capable, and the
    ceiling already lifted 4000 -> 8000, "make the spreadsheet" spent the whole
    8,000 tokens describing the workbook it was going to build, called nothing,
    and truncated with no file. Nothing had actually ASKED for a file — the
    workflow path works precisely because its step prompt does.

    Gated on the tool being available for the model that will answer, never on
    the question alone: telling a model with no code execution to write a file
    to disk instructs it to do something it cannot, and the honest outcome
    there is ordinary prose.

    Threaded into both `question` and `cacheable_system`, the same dual
    injection apply_concise_mode uses and for the same reason: whichever of the
    two a provider path actually sends, the instruction has to be in it.
    APPENDED, like apply_library_context — it is specific to this turn, so it
    belongs after the stable prefix rather than inside it.
    """
    if not wants_artefact:
        return question, cacheable_system
    block = "\n\n".join(artefact_file_instructions())
    new_cacheable_system = (
        f"{cacheable_system}\n\n{block}" if cacheable_system else cacheable_system
    )
    return f"{question}\n\n{block}", new_cacheable_system


def run_orchestrator(
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
    """Route + answer a request.

    `routing_question` is the raw new user turn used ONLY for the routing
    decision (classifier / prefilter / heuristic); the model still answers on
    `req.question` (which may be a full conversation-context prompt). This keeps
    auto mode routing on the actual question instead of the assembled history —
    e.g. a code fence in an earlier turn must not force every later turn to the
    smart tier. Defaults to `req.question` (correct for the stateless endpoint,
    where the two are the same). `owner` is recorded against the call's spend.

    `history` is a short recent-turns snippet used only for the classifier's
    ambiguity check (see decide_route) — when it flags the new turn as
    referentially ambiguous, this returns a clarifying question as the whole
    answer instead of calling any fast/smart model.

    `cacheable_system` (see main.build_context_prompt_with_cache_split) is the
    stable system-prompt/history-summary prefix, threaded to _call_model for
    provider-native prompt caching — see _call_model's docstring.

    `context_free` gates the semantic (paraphrase) cache (see
    app/semantic_cache.py): the caller must explicitly assert `req.question`
    has no conversation history or custom system prompt folded into it —
    defaults False (semantic caching stays off) so a call site that forgets
    to set it just gets today's exact-match-only behavior, never a wrong
    guess from a context-bearing prompt.

    `pre_stage_timings` (see telemetry.StageTimer) folds in durations for
    stages the CALLER already measured before this function was invoked —
    currently cross-conversation memory's and the document library's
    embedding calls, timed in routers/messages.py before run_orchestrator is
    ever reached. Purely for the per-stage latency breakdown in the
    `request.ok` log line; never affects behavior.

    `recall_library` opts this request into RAG document-library recall (see
    app/rag_library.py). Set only by the ask paths — regenerate/edit never
    recall, same reasoning as `remember_memory` in _stream_and_persist.
    Recall happens HERE rather than in the caller because it is gated on the
    task category the router's classifier produces, which nothing outside
    this function knows yet — see _recall_library_context for the gate and
    apply_library_context for how the result reaches the prompt. The
    `[{"document": ..., "snippet_count": ...}]` provenance summary that comes
    back is threaded onto the response's `library_sources` field, same as
    `sources`/citations.

    `memory_sources` (see app/memory.py) is cross-conversation memory's own
    `[{"conversation_title": ..., "created_at": ...}]` summary, computed the
    same way by the caller (_recall_memory, alongside the memory_snippets
    already folded into the prompt) — threaded onto the response's
    `memory_sources` field for the same transparency reason.

    `forced_category` (see app/workflow.py, routing.decide_route) skips
    auto-mode's own classifier call and routes as if it had already
    classified the question into this category — used by workflow mode's
    per-step execution, where the category was already fixed by the
    workflow's planning call.
    """
    meta = new_request_meta()
    timer = StageTimer(meta)
    for stage, duration_ms in (pre_stage_timings or {}).items():
        timer.record(stage, duration_ms)
    route_question = routing_question or req.question

    key = _cache_key(req, owner)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            logger.info(
                "request.cache_hit id=%s ms=%s model=%s",
                meta.request_id,
                ms,
                hit.get("model"),
            )
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": str(hit.get("mode_used") or ""),
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                }
            )
            _record_avoided_cost(owner, hit, "response_cache_hit")
            return _cached_response(hit, meta, ms)
    timer.mark("cache")

    semantic_vector: list[float] | None = None
    if context_free and _cacheable_shape(req) and semantic_cache.enabled():
        semantic_hit, semantic_vector = semantic_cache.find(
            req.question, req.mode.value, owner
        )
        if semantic_hit is not None:
            ms = elapsed_ms(meta)
            logger.info(
                "request.semantic_cache_hit id=%s ms=%s model=%s similarity=%.4f",
                meta.request_id,
                ms,
                semantic_hit.get("model"),
                semantic_hit.get("similarity") or 0.0,
            )
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": str(semantic_hit.get("mode_used") or ""),
                    "ai.model": str(semantic_hit.get("model") or ""),
                    "ai.cache": "semantic_hit",
                }
            )
            _record_avoided_cost(owner, semantic_hit, "semantic_cache_hit")
            return _semantic_cached_response(semantic_hit, meta, ms)
    timer.mark("semantic_cache")

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("request.no_api_key id=%s", meta.request_id)
        return AskResponse(
            answer="",
            mode_used=str(req.mode.value),
            notes=f"{e} | request_id={meta.request_id}",
        )

    decision = decide_route(
        route_question,
        req.mode,
        client=client,
        forced_model=req.model,
        history=history,
        forced_category=forced_category,
        allow_clarify=allow_clarify,
    )

    if _should_auto_workflow(decision, allow_auto_workflow, forced_category):
        # See stream_orchestrator's identical branch for the reasoning and
        # for why this import is lazy.
        from .workflow import run_workflow

        logger.info(
            "ask.auto_workflow id=%s deliverables=%d",
            meta.request_id,
            decision.deliverables,
        )
        return run_workflow(
            req,
            owner=owner,
            auto_routed=True,
            fallback_category=decision.category or None,
            deliverables=decision.deliverables,
        )

    # `require_code_execution` comes from a workflow step's planner verdict.
    # An ORDINARY ask has no planner, so it never reached the ceiling raise
    # below and kept its category's prose-sized cap — which is how a plain
    # "put this into an Excel document" was cut off mid-`tool_use` with no
    # text at all (see _looks_like_artefact_request).
    #
    # The phrase heuristic is for that case ONLY, never for a workflow step
    # (`forced_category` marks one). A workflow already has a per-step verdict,
    # and its steps are handed prompts that quote the original request — so the
    # heuristic would see "spreadsheet" in the SYNTHESIS prompt and promote a
    # step meant for the cheap lane onto a code-capable model. That regression
    # is what test_a_claude_smart_tier_reaches_the_provider_with_code_execution_on
    # caught, and it is precisely the kind of silent cost increase this app
    # exists to make visible.
    # Kept separate from `wants_artefact` below because only THIS half needs
    # the prompt instruction: a workflow step is already told to produce its
    # file by _step_prompt, and saying it twice would just repeat the rules.
    plain_artefact_ask = forced_category is None and _looks_like_artefact_request(
        req.question
    )
    wants_artefact = require_code_execution or plain_artefact_ask
    decision = _apply_code_execution_override(decision, wants_artefact)
    decision = _apply_research_override(decision, req)
    paid_decision = decision
    _, _, _, _, _, _, _, _, _, paid_tools_wanted = _tool_flags_for(
        decision.model, req, decision.needs_live_data
    )
    decision = _apply_free_tier_override(decision, paid_tools_wanted)
    free_tier_active = decision.model != paid_decision.model

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
        }
    )

    logger.info(
        "request.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )
    timer.mark("routing")

    if moderation_enabled():
        # Independent of routing/answering: this checks what the user SENT,
        # not what a model decides to say — the one safety net that doesn't
        # depend on the answering model's own judgment. Checked on
        # route_question (the raw new turn), not req.question (which may be
        # a full conversation-context prompt) — same reasoning decide_route
        # itself uses route_question for. Before budget.reserve: a refused
        # request must not cost anything.
        flagged = check_question(client, route_question)
        if flagged:
            ms = elapsed_ms(meta)
            logger.warning(
                "request.moderation_flagged id=%s categories=%s",
                meta.request_id,
                ",".join(flagged),
            )
            return AskResponse(
                answer="",
                mode_used=decision.mode_used,
                notes=f"{refusal_note(flagged)} | request_id={meta.request_id} | ms={ms}",
            )
    timer.mark("moderation")

    if decision.ambiguous:
        # No model call, no budget reservation, no cache write — the whole
        # point is that this is cheaper than guessing wrong and burning a
        # full answer on the wrong interpretation (see decide_route).
        ms = elapsed_ms(meta)
        logger.info("request.ambiguous id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer=decision.clarifying_question,
            mode_used=decision.mode_used,
            notes=f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
        )

    # propose_action reaches OpenAI and Anthropic (see _ACTION_PROVIDERS);
    # image_generation/code_interpreter stay OpenAI-only — no Anthropic/
    # LiteLLM equivalent wired up here for either of those two. Recomputed
    # against the FINAL decision.model (unchanged from the pre-free-tier
    # computation above unless a substitution happened, in which case a
    # free-tier model never wants any of these — see _apply_free_tier_override).
    (
        actions_wanted,
        images_wanted,
        standalone_image_wanted,
        code_execution_wanted,
        math_solve_wanted,
        fact_check_wanted,
        academic_search_wanted,
        self_describe_tool_wanted,
        self_describe_heuristic_wanted,
        _,
    ) = _tool_flags_for(decision.model, req, decision.needs_live_data)

    refusal, reservation_id = budget.reserve(
        decision.model,
        decision.max_output_tokens,
        req.question,
        _worst_case_image_cost(images_wanted, standalone_image_wanted),
        owner=owner,
    )
    if refusal is not None:
        ms = elapsed_ms(meta)
        logger.warning("request.budget_refused id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"{refusal} | request_id={meta.request_id} | ms={ms}",
            failure_message=refusal,
        )
    timer.mark("budget")

    usage = Usage()
    citations: list[Citation] = []
    search_queries: list[str] = []
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []
    fact_checks: list[dict[str, object]] = []
    academic_results: list[dict[str, object]] = []
    math_results: list[dict[str, object]] = []
    capabilities_calls: list[bool] = []
    # True once a second, facts-in-hand call has answered a textless
    # app_capabilities turn — disclosed in `notes` because it is a second
    # paid call and this app never hides one.
    grounded_note = False

    # Automatic, no-toggle-needed image-token cost reduction (downscaling
    # and/or OCR-replacement — see app/image_processing) applied to whatever
    # was attached, once per request; both the primary call and any fallback
    # below reuse the SAME processed attachments/question rather than
    # re-running OCR per candidate model.
    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
    library_snippets, library_sources = _recall_library_context(
        decision.category, route_question, owner, recall_library, timer
    )
    effective_question, cacheable_system = apply_category_role_prompt(
        decision.category, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_library_context(
        library_snippets, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_concise_mode(
        effective_question, cacheable_system
    )
    # Gated on the tool actually being attached for the model that will answer
    # — the instruction is to WRITE A FILE, which a model without code
    # execution cannot do. See apply_artefact_instructions.
    effective_question, cacheable_system = apply_artefact_instructions(
        plain_artefact_ask and code_execution_wanted,
        effective_question,
        cacheable_system,
    )

    try:
        answer_text = _call_model(
            model=decision.model,
            question=effective_question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
            web_search=decision.needs_live_data,
            citations=citations,
            search_queries=search_queries,
            actions=actions_wanted,
            pending_action=pending_action,
            images=images_wanted,
            generated_images=generated_images,
            attachments=processed_attachments,
            files=req.files,
            truncated=truncated,
            code_execution=code_execution_wanted,
            code_results=code_results,
            math_solve=math_solve_wanted,
            math_results=math_results,
            capabilities=self_describe_tool_wanted,
            capabilities_calls=capabilities_calls,
            cacheable_system=cacheable_system,
            anthropic_question=anthropic_question,
        )
        timer.mark("model_call")

        if standalone_image_wanted:
            standalone_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if standalone_images:
                generated_images.extend(standalone_images)
                answer_text = _compose_answer_with_notes(
                    answer_text, [_image_generation_note(len(standalone_images))]
                )
            else:
                answer_text = _compose_answer_with_notes(
                    answer_text,
                    [_image_generation_failed_note(_image_generation_model())],
                )

        if fact_check_wanted:
            found = check_claim(req.question)
            if found:
                fact_checks.extend(found)
                answer_text = _compose_answer_with_notes(
                    answer_text, [fact_check_note(len(found))]
                )

        if academic_search_wanted:
            papers = search_papers(req.question)
            if papers:
                academic_results.extend(papers)
                answer_text = _compose_answer_with_notes(
                    answer_text, [academic_search_note(len(papers))]
                )

        # self_describe_heuristic_wanted (a LiteLLM-routed model, phrase
        # heuristic) and capabilities_calls (an openai/anthropic model
        # actually called the app_capabilities tool — see _call_model's
        # docstring for why the note is composed HERE, not inside it) are
        # mutually exclusive by construction (see _tool_flags_for), so
        # either firing appends the same real-data note.
        if self_describe_heuristic_wanted or capabilities_calls:
            # include_subsystems only for a self-critique question: the module
            # inventory is ~3,100 tokens and is what stops "suggest
            # improvements" from proposing subsystems this app already has
            # (see app/codebase_inventory.py), but it would be dead weight on
            # "what models do you use".
            self_describe_data = _self_describe_note_safely(
                owner,
                decision.model,
                req,
                decision.needs_live_data,
                include_subsystems=self_describe_improvement_request(
                    effective_question
                ),
            )
            grounded = ""
            if capabilities_calls and not answer_text.strip() and self_describe_data:
                # The tool call and nothing else — the ORDINARY shape for a
                # tool-calling turn, since both providers end the turn on the
                # tool_use block awaiting a result this codebase never sends
                # back. Appending the note then makes it the WHOLE answer: a
                # configuration listing where a question was asked. So answer
                # the question WITH the facts, rather than handing back the
                # facts INSTEAD of an answer (see grounded_question).
                grounded = _self_describe_grounded_answer(
                    decision,
                    effective_question,
                    self_describe_data,
                    usage,
                    cacheable_system,
                )
                grounded_note = bool(grounded)
            answer_text = grounded or _compose_answer_with_notes(
                answer_text, [self_describe_data]
            )
            if not answer_text.strip():
                answer_text = SELF_DESCRIBE_NOTE_FAILED

        # A model that wrote file-producing code as TEXT and then claimed the
        # file exists gets corrected, not repeated — see app/file_claims.py
        # for the live failure. After the other notes (none of them claim
        # files), before the ceiling explanation below.
        if claims_unproduced_file(answer_text, list(code_results)):
            answer_text = _compose_answer_with_notes(
                answer_text, [file_claim_note(code_execution_wanted)]
            )

        # The image twin (see app/image_claims.py). Judged AFTER the image
        # notes above have run, so `generated_images` is final either way.
        if claims_unproduced_image(answer_text, list(generated_images)):
            answer_text = _compose_answer_with_notes(
                answer_text, [image_claim_note(_image_generation_enabled())]
            )

        # Last, once every note above has had its chance to supply text: a call
        # that hit its ceiling with nothing to show explains itself, instead of
        # returning the empty answer the persistence guards drop on the floor
        # (see routers/messages/_shared.py). Without this the user is told only
        # "this question didn't get an answer", with no cause and no cue that
        # retrying verbatim will fail identically.
        no_output = bool(truncated) and not answer_text.strip()
        if no_output:
            answer_text = TRUNCATED_EMPTY_ANSWER

        timer.mark("post_processing")
        ms = elapsed_ms(meta)

        logger.info(
            "request.ok id=%s ms=%s model=%s tokens=%s stages=[%s]",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
            timer.summary(),
        )

        image_cost = (
            estimate_image_cost(len(generated_images), _image_generation_quality())
            if generated_images
            else None
        )
        code_execution_cost = estimate_code_execution_cost(len(code_results))
        extra_cost = (image_cost or 0.0) + (code_execution_cost or 0.0)
        notes = f"{decision.notes} | request_id={meta.request_id} | ms={ms}"
        if grounded_note:
            notes = f"{notes} | grounded self-describe (second call)"
        if image_note:
            notes = f"{notes} | {image_note}"
        response = AskResponse(
            answer=answer_text,
            mode_used=decision.mode_used,
            model=decision.model,
            notes=notes,
            sources=[Source(**c) for c in citations] or None,
            search_queries=search_queries or None,
            pending_action=(
                PendingAction.model_validate(pending_action[0])
                if pending_action
                else None
            ),
            images=generated_images or None,
            code_results=[CodeResult.model_validate(c) for c in code_results] or None,
            fact_checks=[FactCheck.model_validate(c) for c in fact_checks] or None,
            academic_results=[
                AcademicResult.model_validate(a) for a in academic_results
            ]
            or None,
            math_results=[MathResult.model_validate(m) for m in math_results] or None,
            library_sources=(
                [LibrarySource(**s) for s in library_sources]
                if library_sources
                else None
            ),
            memory_sources=(
                [MemorySource(**s) for s in memory_sources] if memory_sources else None
            ),
            truncated=bool(truncated),
            max_output_tokens=decision.max_output_tokens,
            no_output=no_output,
            # The app's own capabilities snapshot was appended to this answer
            # (see the self_describe branch above), so it carries live
            # per-owner account state — remaining daily budget, free-lane
            # quotas, the effective model map. Same reason `cacheable_answer`
            # below refuses to cache it; see AskResponse.memorable.
            memorable=not (self_describe_heuristic_wanted or capabilities_calls),
            **_usage_fields(decision.model, usage, extra_cost),
        )
        # A freshness-sensitive answer must not be frozen into the cache — a
        # later identical prompt would replay stale "current" info instead of
        # searching again. Only cache non-web-search answers. Same for a
        # proposed action, a generated image, executed code, a fact-check
        # lookup, or a math_solve result: the cache has no column to store
        # any of them, so a cached hit would silently drop them, and
        # replaying a stale action proposal the client already resolved
        # would be actively wrong. Shared by both cache backends; each
        # backend's OWN enabled-check (cache.enabled() via `key is not
        # None`, semantic_cache.enabled()) is independent, so one being off
        # never gates the other.
        cacheable_answer = (
            not decision.needs_live_data
            and not pending_action
            and not generated_images
            and not code_results
            and not fact_checks
            and not academic_results
            and not math_results
            and not self_describe_heuristic_wanted
            and not capabilities_calls
            # A cut-off answer is an incomplete one. Freezing it in would serve
            # the same half-answer — or the bare "I ran out of output space"
            # explanation — to every later asker of this question, for free and
            # forever.
            and not truncated
        )
        if key is not None and cacheable_answer:
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_text,
                decision.mode_used,
                response.notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        if context_free and cacheable_answer and semantic_cache.enabled():
            semantic_cache.put(
                req.question,
                req.mode.value,
                owner,
                semantic_vector,
                answer_text,
                decision.mode_used,
                response.notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        _record_spend(owner, decision.model, usage, extra_cost, reservation_id)
        if free_tier_active:
            _record_free_tier_avoided_cost(owner, paid_decision.model, usage)
        return response

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("request.auth_failed id=%s ms=%s", meta.request_id, ms)
        budget.release(reservation_id)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
        )

    except Exception as primary_error:
        # A rate-limit / quota error means the primary's key is throttled, so
        # only a DIFFERENT provider can help — fail over cross-vendor only.
        rate_limited = isinstance(primary_error, RATE_ERRORS)
        reason = classify_error_reason(primary_error)
        logger.exception(
            "request.primary_model_failed id=%s model=%s err=%s rate_limited=%s reason=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
            rate_limited,
            reason,
        )
        # The primary attempt is over (success or fail, no in-between): settle
        # its reservation now rather than carrying it into the fallback loop.
        _record_spend(owner, decision.model, usage, reservation_id=reservation_id)

        # Whether ANY fallback candidate actually got dispatched (passed its
        # own budget.reserve() gate) — see the BUDGET_REFUSAL override below.
        any_fallback_dispatched = False
        fallbacks = _fallback_models(decision.model, cross_provider_only=rate_limited)
        if free_tier_active:
            # The picked free-tier model just failed — cool it down for the
            # rest of the UTC day (see free_tier.exhaust_for_today) and try
            # the REMAINING free candidates (in FREE_TIER_MODELS order) before
            # the original paid model and the normal cross-vendor chain, per
            # _apply_free_tier_override's "fall through to the next free
            # candidate, then normal paid routing" contract.
            free_tier.exhaust_for_today(decision.model)
            ordered = [
                *free_tier.remaining_candidates_after(decision.model),
                paid_decision.model,
                *fallbacks,
            ]
            seen_fallback: set[str] = set()
            fallbacks = []
            for candidate in ordered:
                if candidate != decision.model and candidate not in seen_fallback:
                    seen_fallback.add(candidate)
                    fallbacks.append(candidate)

        for fallback_model in fallbacks:
            # Every hosted tool, re-derived for the model about to be called —
            # never inherited from the primary. See _FallbackTools.
            tools = _fallback_tools(fallback_model, req, decision.needs_live_data)
            # The pre-dispatch gate ran against the PRIMARY model, whose worst
            # case may have been $0 (a free local Ollama primary that turned
            # out to be down). Re-gate each fallback candidate so the failure
            # of a free model can't route PAID spend past an exhausted cap.
            # Image generation is priced in for the same reason the primary
            # prices it: it is real money the token estimate cannot see.
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model,
                decision.max_output_tokens,
                req.question,
                _worst_case_image_cost(tools.images, tools.standalone_image),
                owner=owner,
            )
            if fallback_refusal is not None:
                logger.warning(
                    "request.fallback_budget_refused id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )
                continue
            any_fallback_dispatched = True
            # A remaining free-tier candidate tried as a fallback gets the
            # same decision-time quota recording as the original pick (see
            # _apply_free_tier_override) — recorded up front, cooled down on
            # failure (below), and its avoided cost logged on success.
            fallback_is_free = free_tier_active and free_tier.is_free_tier_model(
                fallback_model
            )
            if fallback_is_free:
                free_tier.record_use(fallback_model)
            try:
                logger.info(
                    "request.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                fallback_usage = Usage()
                fallback_truncated: list[bool] = []
                answer_text = _call_model(
                    model=fallback_model,
                    question=effective_question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                    web_search=tools.web_search,
                    citations=tools.citations,
                    search_queries=tools.search_queries,
                    actions=tools.actions,
                    pending_action=tools.pending_action,
                    images=tools.images,
                    generated_images=tools.generated_images,
                    # Vision/file attachments are threaded to every provider
                    # path — dropping the user's image or document on fallback
                    # would silently lose context they explicitly provided.
                    attachments=processed_attachments,
                    files=req.files,
                    truncated=fallback_truncated,
                    code_execution=tools.code_execution,
                    code_results=tools.code_results,
                    math_solve=tools.math_solve,
                    math_results=tools.math_results,
                    capabilities=tools.capabilities,
                    capabilities_calls=tools.capabilities_calls,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                )

                # The same post-call work the primary path does, for the same
                # reasons — see its copy above. Gemini image generation and the
                # fact-check / academic lookups are separate calls rather than
                # hosted tools, so they were being skipped on fallback purely
                # by sharing this code path.
                if tools.standalone_image:
                    standalone_images = generate_images_litellm(
                        _image_generation_model(),
                        req.question,
                        _image_generation_quality(),
                        _image_generation_size(),
                    )
                    if standalone_images:
                        tools.generated_images.extend(standalone_images)
                        answer_text = _compose_answer_with_notes(
                            answer_text,
                            [_image_generation_note(len(standalone_images))],
                        )
                    else:
                        answer_text = _compose_answer_with_notes(
                            answer_text,
                            [_image_generation_failed_note(_image_generation_model())],
                        )
                if tools.fact_check:
                    found = check_claim(req.question)
                    if found:
                        tools.fact_checks.extend(found)
                        answer_text = _compose_answer_with_notes(
                            answer_text, [fact_check_note(len(found))]
                        )
                if tools.academic_search:
                    papers = search_papers(req.question)
                    if papers:
                        tools.academic_results.extend(papers)
                        answer_text = _compose_answer_with_notes(
                            answer_text, [academic_search_note(len(papers))]
                        )
                # Mutually exclusive by construction (see _tool_flags_for);
                # either firing appends the same real-data note.
                if tools.self_describe_heuristic or tools.capabilities_calls:
                    answer_text = _compose_answer_with_notes(
                        answer_text,
                        [
                            _self_describe_note_safely(
                                owner, fallback_model, req, tools.web_search
                            )
                        ],
                    )

                # Same correction as the primary path: a fallback's claimed
                # file is no more real (see app/file_claims.py).
                if claims_unproduced_file(answer_text, list(tools.code_results)):
                    answer_text = _compose_answer_with_notes(
                        answer_text, [file_claim_note(tools.code_execution)]
                    )

                # Same correction as the primary path: a fallback's claimed
                # image is no more real (see app/image_claims.py).
                if claims_unproduced_image(answer_text, list(tools.generated_images)):
                    answer_text = _compose_answer_with_notes(
                        answer_text,
                        [image_claim_note(_image_generation_enabled())],
                    )

                # Last, as in the primary path: a fallback that hit its ceiling
                # with nothing to show explains itself. Both primary paths did
                # this and neither fallback did, so the WORSE case — two models
                # paid for, one of them a cross-vendor retry — was the one that
                # returned a bare empty answer, dropped by the persistence
                # guards as "not saved (empty answer)" with no cause given and
                # no cue that retrying verbatim fails identically. no_output
                # also drives the UI's Retry-as-workflow affordance, so leaving
                # it False withheld the one remedy that actually works.
                fallback_no_output = bool(fallback_truncated) and not (
                    answer_text.strip()
                )
                if fallback_no_output:
                    answer_text = TRUNCATED_EMPTY_ANSWER

                ms = elapsed_ms(meta)

                logger.info(
                    "request.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_notes = (
                    f"{decision.notes} | primary_model={decision.model} failed with "
                    f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                    f"| fallback_reason={REASON_LABELS[reason]} "
                    f"| request_id={meta.request_id} | ms={ms}"
                )
                if image_note:
                    fallback_notes = f"{fallback_notes} | {image_note}"
                # Every extra cost this call really incurred, priced the way
                # the primary path prices its own — neither is visible to
                # token pricing, so omitting either lets the daily cap drift.
                fallback_extra_cost = (
                    estimate_code_execution_cost(len(tools.code_results)) or 0.0
                ) + (
                    estimate_image_cost(
                        len(tools.generated_images), _image_generation_quality()
                    )
                    or 0.0
                    if tools.generated_images
                    else 0.0
                )
                fallback_response = AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=fallback_notes,
                    model=fallback_model,
                    truncated=bool(fallback_truncated),
                    no_output=fallback_no_output,
                    # The fallback ran under the primary decision's ceiling (see
                    # the _call_model above), so it is the same number.
                    max_output_tokens=decision.max_output_tokens,
                    sources=[Source(**c) for c in tools.citations] or None,
                    search_queries=tools.search_queries or None,
                    pending_action=(
                        PendingAction.model_validate(tools.pending_action[0])
                        if tools.pending_action
                        else None
                    ),
                    images=tools.generated_images or None,
                    code_results=[
                        CodeResult.model_validate(c) for c in tools.code_results
                    ]
                    or None,
                    fact_checks=[FactCheck.model_validate(c) for c in tools.fact_checks]
                    or None,
                    academic_results=[
                        AcademicResult.model_validate(a) for a in tools.academic_results
                    ]
                    or None,
                    math_results=[
                        MathResult.model_validate(m) for m in tools.math_results
                    ]
                    or None,
                    # Recalled BEFORE the primary call and already folded into
                    # the prompt this fallback was given (see
                    # _recall_library_context / apply_library_context), so the
                    # fallback answer really did draw on these documents and
                    # past conversations. Omitting them said otherwise —
                    # the transparency the fields exist for, missing on
                    # exactly the answers a user is most likely to question.
                    library_sources=(
                        [LibrarySource(**s) for s in library_sources]
                        if library_sources
                        else None
                    ),
                    memory_sources=(
                        [MemorySource(**s) for s in memory_sources]
                        if memory_sources
                        else None
                    ),
                    # Same guard the primary path applies, and it became
                    # load-bearing here only now: the fallback can append the
                    # capabilities snapshot, which carries live per-owner
                    # account state (remaining budget, free-lane quotas, the
                    # effective model map). `memorable` defaults to True, so
                    # omitting it would write that snapshot into durable
                    # cross-conversation memory. See AskResponse.memorable.
                    memorable=not (
                        tools.self_describe_heuristic or tools.capabilities_calls
                    ),
                    **_usage_fields(
                        fallback_model, fallback_usage, fallback_extra_cost
                    ),
                )
                # The primary path's own list, read off THIS call's collectors
                # — see _FallbackTools.cacheable, including why the old check
                # consulting the PRIMARY's lists was wrong even before the
                # fallback had tools of its own.
                fallback_cacheable_answer = tools.cacheable(decision.needs_live_data)
                if key is not None and fallback_cacheable_answer:
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        answer_text,
                        fallback_response.mode_used,
                        fallback_response.notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                if (
                    context_free
                    and fallback_cacheable_answer
                    and semantic_cache.enabled()
                ):
                    semantic_cache.put(
                        req.question,
                        req.mode.value,
                        owner,
                        semantic_vector,
                        answer_text,
                        fallback_response.mode_used,
                        fallback_response.notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    fallback_extra_cost,
                    reservation_id=fallback_reservation_id,
                )
                if fallback_is_free:
                    _record_free_tier_avoided_cost(
                        owner, paid_decision.model, fallback_usage
                    )
                _record_fallback_event(owner, decision.model, reason, succeeded=True)
                return fallback_response

            except Exception as fallback_error:
                logger.exception(
                    "request.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )
                if fallback_is_free:
                    free_tier.exhaust_for_today(fallback_model)

        ms = elapsed_ms(meta)

        # Every candidate got budget-refused (never dispatched at all): the
        # operative cause of ending up empty-handed is the daily budget, not
        # the primary's own (possibly transient/unrelated) original error.
        final_reason = (
            BUDGET_REFUSAL if fallbacks and not any_fallback_dispatched else reason
        )
        _record_fallback_event(owner, decision.model, final_reason, succeeded=False)

        if rate_limited:
            notes = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| fallback_reason={REASON_LABELS[final_reason]} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            notes = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| fallback_reason={REASON_LABELS[final_reason]} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=notes,
            failure_message=_fallback_exhausted_failure_message(primary_error, ms),
        )


def stream_orchestrator(
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
) -> Generator[dict[str, Any], None, None]:
    """
    Streaming variant of run_orchestrator.

    Yields plain dicts {"event": str, "data": dict} matching the SSE contract:
    one "meta" event, zero or more "delta" events, then a terminal "done" or
    "error" event. Persistence and wire formatting are the caller's job; this
    function never touches the message store (it does append spend-log rows).

    `routing_question` routes on the raw new user turn while the model answers
    on `req.question`; see run_orchestrator. `owner` is recorded against spend.
    `history` feeds the classifier's ambiguity check, same as run_orchestrator.
    `cacheable_system` is threaded to _stream_model for provider-native prompt
    caching; see run_orchestrator/_call_model's docstrings. `context_free`
    gates the semantic cache exactly as in run_orchestrator — see its
    docstring. `pre_stage_timings` folds in stages the caller already timed
    before this generator started — see run_orchestrator's docstring.
    `recall_library` opts into category-gated RAG document-library recall,
    identically to run_orchestrator — see its docstring and
    _recall_library_context.
    """
    meta = new_request_meta()
    timer = StageTimer(meta)
    for stage, duration_ms in (pre_stage_timings or {}).items():
        timer.record(stage, duration_ms)
    route_question = routing_question or req.question

    key = _cache_key(req, owner)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            answer = str(hit.get("answer") or "")
            mode_used = str(hit.get("mode_used") or "cache")
            logger.info("stream.cache_hit id=%s ms=%s", meta.request_id, ms)
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": mode_used,
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                    "ai.streaming": True,
                }
            )
            _record_avoided_cost(owner, hit, "response_cache_hit")
            yield {
                "event": "meta",
                "data": {
                    "request_id": meta.request_id,
                    "mode_used": mode_used,
                    "model": str(hit.get("model") or ""),
                    "notes": "cache=hit",
                },
            }
            if answer:
                yield {"event": "delta", "data": {"text": answer}}
            yield {
                "event": "done",
                "data": {
                    "deployment_id": db_deployment_id(),
                    "answer": answer,
                    "mode_used": mode_used,
                    "notes": _cached_hit_note(hit, meta, ms),
                    "cached": True,
                },
            }
            return
    timer.mark("cache")

    semantic_vector: list[float] | None = None
    if context_free and _cacheable_shape(req) and semantic_cache.enabled():
        semantic_hit, semantic_vector = semantic_cache.find(
            req.question, req.mode.value, owner
        )
        if semantic_hit is not None:
            ms = elapsed_ms(meta)
            answer = str(semantic_hit.get("answer") or "")
            mode_used = str(semantic_hit.get("mode_used") or "semantic_cache")
            logger.info(
                "stream.semantic_cache_hit id=%s ms=%s similarity=%.4f",
                meta.request_id,
                ms,
                semantic_hit.get("similarity") or 0.0,
            )
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": mode_used,
                    "ai.model": str(semantic_hit.get("model") or ""),
                    "ai.cache": "semantic_hit",
                    "ai.streaming": True,
                }
            )
            _record_avoided_cost(owner, semantic_hit, "semantic_cache_hit")
            yield {
                "event": "meta",
                "data": {
                    "request_id": meta.request_id,
                    "mode_used": mode_used,
                    "model": str(semantic_hit.get("model") or ""),
                    "notes": "cache=semantic_hit",
                },
            }
            if answer:
                yield {"event": "delta", "data": {"text": answer}}
            yield {
                "event": "done",
                "data": {
                    "deployment_id": db_deployment_id(),
                    "answer": answer,
                    "mode_used": mode_used,
                    "notes": _semantic_cached_hit_note(semantic_hit, meta, ms),
                    "cached": True,
                },
            }
            return
    timer.mark("semantic_cache")

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("stream.no_api_key id=%s", meta.request_id)
        yield {"event": "error", "data": {"message": str(e)}}
        return

    decision = decide_route(
        route_question,
        req.mode,
        client=client,
        forced_model=req.model,
        history=history,
        forced_category=forced_category,
        allow_clarify=allow_clarify,
    )

    if _should_auto_workflow(decision, allow_auto_workflow, forced_category):
        # The classifier just told us this asks for several distinct
        # artefacts. Hand the whole request to workflow mode rather than
        # answering it single-shot — the decision the user would otherwise
        # have had to make by hand, taken from the classification the router
        # already paid for. Imported lazily: workflow.py imports THIS module
        # at module level, so a top-level import here would be circular.
        from .workflow import stream_workflow

        logger.info(
            "stream.auto_workflow id=%s deliverables=%d",
            meta.request_id,
            decision.deliverables,
        )
        yield from stream_workflow(
            req,
            owner=owner,
            auto_routed=True,
            fallback_category=decision.category or None,
            deliverables=decision.deliverables,
        )
        return

    # `require_code_execution` comes from a workflow step's planner verdict.
    # An ORDINARY ask has no planner, so it never reached the ceiling raise
    # below and kept its category's prose-sized cap — which is how a plain
    # "put this into an Excel document" was cut off mid-`tool_use` with no
    # text at all (see _looks_like_artefact_request).
    #
    # The phrase heuristic is for that case ONLY, never for a workflow step
    # (`forced_category` marks one). A workflow already has a per-step verdict,
    # and its steps are handed prompts that quote the original request — so the
    # heuristic would see "spreadsheet" in the SYNTHESIS prompt and promote a
    # step meant for the cheap lane onto a code-capable model. That regression
    # is what test_a_claude_smart_tier_reaches_the_provider_with_code_execution_on
    # caught, and it is precisely the kind of silent cost increase this app
    # exists to make visible.
    # Kept separate from `wants_artefact` below because only THIS half needs
    # the prompt instruction: a workflow step is already told to produce its
    # file by _step_prompt, and saying it twice would just repeat the rules.
    plain_artefact_ask = forced_category is None and _looks_like_artefact_request(
        req.question
    )
    wants_artefact = require_code_execution or plain_artefact_ask
    decision = _apply_code_execution_override(decision, wants_artefact)
    decision = _apply_research_override(decision, req)
    paid_decision = decision
    _, _, _, _, _, _, _, _, _, paid_tools_wanted = _tool_flags_for(
        decision.model, req, decision.needs_live_data
    )
    decision = _apply_free_tier_override(decision, paid_tools_wanted)
    free_tier_active = decision.model != paid_decision.model

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
            "ai.streaming": True,
        }
    )

    logger.info(
        "stream.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )
    timer.mark("routing")

    if moderation_enabled():
        flagged = check_question(client, route_question)
        if flagged:
            ms = elapsed_ms(meta)
            logger.warning(
                "stream.moderation_flagged id=%s categories=%s",
                meta.request_id,
                ",".join(flagged),
            )
            # Refuse before any model work — no meta, matching the
            # no-api-key/budget-refusal paths.
            yield {
                "event": "error",
                "data": {
                    "message": f"{refusal_note(flagged)} | request_id={meta.request_id} | ms={ms}"
                },
            }
            return
    timer.mark("moderation")

    if decision.ambiguous:
        # Same short-circuit as run_orchestrator, in SSE shape: one delta with
        # the whole clarifying question, then done — no model call at all.
        ms = elapsed_ms(meta)
        logger.info("stream.ambiguous id=%s ms=%s", meta.request_id, ms)
        yield {
            "event": "meta",
            "data": {
                "request_id": meta.request_id,
                "mode_used": decision.mode_used,
                "model": decision.model,
                "notes": decision.notes,
            },
        }
        yield {"event": "delta", "data": {"text": decision.clarifying_question}}
        yield {
            "event": "done",
            "data": {
                "deployment_id": db_deployment_id(),
                "answer": decision.clarifying_question,
                "mode_used": decision.mode_used,
                "notes": f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    (
        actions_wanted,
        images_wanted,
        standalone_image_wanted,
        code_execution_wanted,
        math_solve_wanted,
        fact_check_wanted,
        academic_search_wanted,
        self_describe_tool_wanted,
        self_describe_heuristic_wanted,
        _,
    ) = _tool_flags_for(decision.model, req, decision.needs_live_data)

    refusal, reservation_id = budget.reserve(
        decision.model,
        decision.max_output_tokens,
        req.question,
        _worst_case_image_cost(images_wanted, standalone_image_wanted),
        owner=owner,
    )
    if refusal is not None:
        ms = elapsed_ms(meta)
        logger.warning("stream.budget_refused id=%s ms=%s", meta.request_id, ms)
        # Refuse before any model work — no meta, matching the no-api-key path.
        yield {
            "event": "error",
            "data": {
                "message": f"{refusal} | request_id={meta.request_id} | ms={ms}",
                "failure_message": refusal,
            },
        }
        return
    timer.mark("budget")

    yield {
        "event": "meta",
        "data": {
            "request_id": meta.request_id,
            "mode_used": decision.mode_used,
            "model": decision.model,
            "notes": decision.notes,
        },
    }

    streamed_any = False
    accumulated: list[str] = []
    usage = Usage()
    citations: list[Citation] = []
    search_queries: list[str] = []
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []
    fact_checks: list[dict[str, object]] = []
    academic_results: list[dict[str, object]] = []
    math_results: list[dict[str, object]] = []
    capabilities_calls: list[bool] = []
    # True once a second, facts-in-hand call has answered a textless
    # app_capabilities turn — disclosed in `notes` because it is a second
    # paid call and this app never hides one.
    grounded_note = False

    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
    library_snippets, library_sources = _recall_library_context(
        decision.category, route_question, owner, recall_library, timer
    )
    effective_question, cacheable_system = apply_category_role_prompt(
        decision.category, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_library_context(
        library_snippets, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_concise_mode(
        effective_question, cacheable_system
    )
    # Gated on the tool actually being attached for the model that will answer
    # — the instruction is to WRITE A FILE, which a model without code
    # execution cannot do. See apply_artefact_instructions.
    effective_question, cacheable_system = apply_artefact_instructions(
        plain_artefact_ask and code_execution_wanted,
        effective_question,
        cacheable_system,
    )

    try:
        for text in _stream_model(
            model=decision.model,
            question=effective_question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
            web_search=decision.needs_live_data,
            citations=citations,
            search_queries=search_queries,
            actions=actions_wanted,
            pending_action=pending_action,
            images=images_wanted,
            generated_images=generated_images,
            attachments=processed_attachments,
            files=req.files,
            truncated=truncated,
            code_execution=code_execution_wanted,
            code_results=code_results,
            math_solve=math_solve_wanted,
            math_results=math_results,
            capabilities=self_describe_tool_wanted,
            capabilities_calls=capabilities_calls,
            cacheable_system=cacheable_system,
            anthropic_question=anthropic_question,
        ):
            streamed_any = True
            accumulated.append(text)
            yield {"event": "delta", "data": {"text": text}}
        timer.mark("model_call")

        if standalone_image_wanted:
            standalone_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if standalone_images:
                generated_images.extend(standalone_images)
                note = _image_generation_note(len(standalone_images))
            else:
                note = _image_generation_failed_note(_image_generation_model())
            note_text = note if not accumulated else f"\n\n{note}"
            accumulated.append(note_text)
            streamed_any = True
            yield {"event": "delta", "data": {"text": note_text}}

        if fact_check_wanted:
            found = check_claim(req.question)
            if found:
                fact_checks.extend(found)
                note = fact_check_note(len(found))
                note_text = note if not accumulated else f"\n\n{note}"
                accumulated.append(note_text)
                streamed_any = True
                yield {"event": "delta", "data": {"text": note_text}}

        if academic_search_wanted:
            papers = search_papers(req.question)
            if papers:
                academic_results.extend(papers)
                note = academic_search_note(len(papers))
                note_text = note if not accumulated else f"\n\n{note}"
                accumulated.append(note_text)
                streamed_any = True
                yield {"event": "delta", "data": {"text": note_text}}

        if self_describe_heuristic_wanted or capabilities_calls:
            # See run_orchestrator's twin for why the inventory is gated.
            note = _self_describe_note_safely(
                owner,
                decision.model,
                req,
                decision.needs_live_data,
                include_subsystems=self_describe_improvement_request(
                    effective_question
                ),
            )
            grounded = ""
            if capabilities_calls and not "".join(accumulated).strip() and note:
                # See run_orchestrator: answer the question with the facts,
                # rather than handing back the facts instead of an answer. Not
                # streamed incrementally — this is a whole second call made
                # after the first one finished, so it arrives as one delta.
                grounded = _self_describe_grounded_answer(
                    decision, effective_question, note, usage, cacheable_system
                )
                grounded_note = bool(grounded)
            note_text = grounded or note
            if not note_text and not "".join(accumulated).strip():
                note_text = SELF_DESCRIBE_NOTE_FAILED
            if note_text:
                if accumulated:
                    note_text = f"\n\n{note_text}"
                accumulated.append(note_text)
                streamed_any = True
                yield {"event": "delta", "data": {"text": note_text}}

        # A model that wrote file-producing code as TEXT and then claimed
        # the file exists gets corrected, not repeated — run_orchestrator's
        # twin, streamed as one delta (see app/file_claims.py). Judged on
        # the accumulated model text; the notes above never claim files.
        if claims_unproduced_file("".join(accumulated), list(code_results)):
            note = file_claim_note(code_execution_wanted)
            note_text = note if not accumulated else f"\n\n{note}"
            accumulated.append(note_text)
            streamed_any = True
            yield {"event": "delta", "data": {"text": note_text}}

        # The image twin, streamed the same way (see app/image_claims.py).
        if claims_unproduced_image("".join(accumulated), list(generated_images)):
            note = image_claim_note(_image_generation_enabled())
            note_text = note if not accumulated else f"\n\n{note}"
            accumulated.append(note_text)
            streamed_any = True
            yield {"event": "delta", "data": {"text": note_text}}

        # Last, once every note above has had its chance to supply text — see
        # run_orchestrator's twin. Streamed as a delta too, so a waiting UI
        # resolves into the explanation instead of simply stopping.
        no_output = bool(truncated) and not "".join(accumulated).strip()
        if no_output:
            accumulated.append(TRUNCATED_EMPTY_ANSWER)
            streamed_any = True
            yield {"event": "delta", "data": {"text": TRUNCATED_EMPTY_ANSWER}}

        timer.mark("post_processing")
        ms = elapsed_ms(meta)

        logger.info(
            "stream.ok id=%s ms=%s model=%s tokens=%s stages=[%s]",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
            timer.summary(),
        )

        answer_final = "".join(accumulated).strip()
        done_notes = f"{decision.notes} | request_id={meta.request_id} | ms={ms}"
        if grounded_note:
            done_notes = f"{done_notes} | grounded self-describe (second call)"
        if image_note:
            done_notes = f"{done_notes} | {image_note}"
        image_cost = (
            estimate_image_cost(len(generated_images), _image_generation_quality())
            if generated_images
            else None
        )
        code_execution_cost = estimate_code_execution_cost(len(code_results))
        extra_cost = (image_cost or 0.0) + (code_execution_cost or 0.0)
        # See run_orchestrator: a freshness-sensitive answer, one with a
        # proposed action, a generated image, executed code, a fact-check
        # lookup, or a math_solve result is never cached, in either backend.
        # Both gated independently.
        cacheable_answer = (
            not decision.needs_live_data
            and not pending_action
            and not generated_images
            and not code_results
            and not fact_checks
            and not academic_results
            and not math_results
            and not self_describe_heuristic_wanted
            and not capabilities_calls
            # A cut-off answer is an incomplete one. Freezing it in would serve
            # the same half-answer — or the bare "I ran out of output space"
            # explanation — to every later asker of this question, for free and
            # forever.
            and not truncated
        )
        if key is not None and cacheable_answer:
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_final,
                decision.mode_used,
                done_notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        if context_free and cacheable_answer and semantic_cache.enabled():
            semantic_cache.put(
                req.question,
                req.mode.value,
                owner,
                semantic_vector,
                answer_final,
                decision.mode_used,
                done_notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        # Record spend even when answer_final is empty (truncated call): the
        # tokens were still billed, so the budget must see them.
        _record_spend(owner, decision.model, usage, extra_cost, reservation_id)
        if free_tier_active:
            _record_free_tier_avoided_cost(owner, paid_decision.model, usage)

        yield {
            "event": "done",
            "data": {
                "deployment_id": db_deployment_id(),
                "answer": answer_final,
                "mode_used": decision.mode_used,
                "notes": done_notes,
                "model": decision.model,
                "truncated": bool(truncated),
                "max_output_tokens": decision.max_output_tokens,
                "no_output": no_output,
                **({"sources": citations} if citations else {}),
                **({"search_queries": search_queries} if search_queries else {}),
                **({"pending_action": pending_action[0]} if pending_action else {}),
                **({"images": generated_images} if generated_images else {}),
                **({"code_results": code_results} if code_results else {}),
                **({"fact_checks": fact_checks} if fact_checks else {}),
                **({"academic_results": academic_results} if academic_results else {}),
                **({"math_results": math_results} if math_results else {}),
                **({"library_sources": library_sources} if library_sources else {}),
                **({"memory_sources": memory_sources} if memory_sources else {}),
                # Streaming twin of AskResponse.memorable — see that field.
                # Present only when the answer must NOT be remembered, and
                # consumed (and removed) by _run_ask_stream_worker before the
                # frame reaches the client, so the SSE contract is unchanged.
                **(
                    {"memorable": False}
                    if (self_describe_heuristic_wanted or capabilities_calls)
                    else {}
                ),
                **_usage_fields(decision.model, usage, extra_cost),
            },
        }
        return

    except GeneratorExit:
        # The client disconnected (Stop button, tab close, network drop)
        # mid-stream — Starlette closes this generator, which raises
        # GeneratorExit at the `yield` above. It's a BaseException, so the
        # `except Exception` below never sees it; without this clause the
        # provider had already billed these tokens but the budget never
        # would have found out. Record what was spent, then propagate the
        # close (a generator must never swallow GeneratorExit).
        if usage.total_tokens:
            logger.warning(
                "stream.client_disconnected id=%s model=%s tokens=%s",
                meta.request_id,
                decision.model,
                usage.total_tokens,
            )
            _record_spend(owner, decision.model, usage, reservation_id=reservation_id)
        else:
            budget.release(reservation_id)
        raise

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("stream.auth_failed id=%s ms=%s", meta.request_id, ms)
        budget.release(reservation_id)
        yield {
            "event": "error",
            "data": {
                "message": f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    except Exception as primary_error:
        # Rate-limit / quota: the same key stays throttled, so fail over to a
        # DIFFERENT provider only (if one is configured).
        rate_limited = isinstance(primary_error, RATE_ERRORS)
        reason = classify_error_reason(primary_error)
        # The primary attempt is over (success, partial stream, or clean
        # failure): settle its reservation either way before doing anything else.
        _record_spend(owner, decision.model, usage, reservation_id=reservation_id)
        if streamed_any:
            # Partial output already went out; no fallback is possible.
            ms = elapsed_ms(meta)
            logger.exception(
                "stream.interrupted id=%s ms=%s model=%s err=%s",
                meta.request_id,
                ms,
                decision.model,
                type(primary_error).__name__,
            )
            yield {
                "event": "error",
                "data": {
                    "message": (
                        f"Stream interrupted: {type(primary_error).__name__}: {primary_error} "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                },
            }
            return

        logger.exception(
            "stream.primary_model_failed id=%s model=%s err=%s reason=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
            reason,
        )

        any_fallback_dispatched = False
        fallbacks = _fallback_models(decision.model, cross_provider_only=rate_limited)
        if free_tier_active:
            # Same "cool the failed free model down, try the remaining free
            # candidates before the original paid model and the normal
            # cross-vendor chain" logic as run_orchestrator's fallback loop.
            free_tier.exhaust_for_today(decision.model)
            ordered = [
                *free_tier.remaining_candidates_after(decision.model),
                paid_decision.model,
                *fallbacks,
            ]
            seen_fallback: set[str] = set()
            fallbacks = []
            for candidate in ordered:
                if candidate != decision.model and candidate not in seen_fallback:
                    seen_fallback.add(candidate)
                    fallbacks.append(candidate)

        for fallback_model in fallbacks:
            # See run_orchestrator's fallback loop for both of these: every
            # hosted tool re-derived for the model about to be called, and the
            # re-gate (the primary's pre-dispatch check may have priced at $0
            # for a free local model, so each paid candidate clears the budget
            # itself — image generation included).
            tools = _fallback_tools(fallback_model, req, decision.needs_live_data)
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model,
                decision.max_output_tokens,
                req.question,
                _worst_case_image_cost(tools.images, tools.standalone_image),
                owner=owner,
            )
            if fallback_refusal is not None:
                logger.warning(
                    "stream.fallback_budget_refused id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )
                continue
            any_fallback_dispatched = True
            fallback_is_free = free_tier_active and free_tier.is_free_tier_model(
                fallback_model
            )
            if fallback_is_free:
                free_tier.record_use(fallback_model)
            fallback_parts: list[str] = []
            fallback_usage = Usage()
            fallback_truncated: list[bool] = []

            try:
                logger.info(
                    "stream.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                for text in _stream_model(
                    model=fallback_model,
                    question=effective_question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                    web_search=tools.web_search,
                    citations=tools.citations,
                    search_queries=tools.search_queries,
                    actions=tools.actions,
                    pending_action=tools.pending_action,
                    images=tools.images,
                    generated_images=tools.generated_images,
                    # Vision/file attachments work across every provider, so
                    # they are kept on fallback too — dropping them would lose
                    # context the user explicitly provided.
                    attachments=processed_attachments,
                    files=req.files,
                    truncated=fallback_truncated,
                    code_execution=tools.code_execution,
                    code_results=tools.code_results,
                    math_solve=tools.math_solve,
                    math_results=tools.math_results,
                    capabilities=tools.capabilities,
                    capabilities_calls=tools.capabilities_calls,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                ):
                    fallback_parts.append(text)
                    yield {"event": "delta", "data": {"text": text}}

                # The same post-call work the streaming PRIMARY does, in the
                # same shape: each note is appended to the accumulated answer
                # AND streamed as its own delta, so a reader watching the
                # answer arrive sees it rather than having it appear only in
                # the persisted text. Gemini image generation and the
                # fact-check / academic lookups are separate calls rather than
                # hosted tools, so they were being skipped on fallback purely
                # by sharing this code path.
                fallback_notes_to_stream: list[str] = []
                if tools.standalone_image:
                    standalone_images = generate_images_litellm(
                        _image_generation_model(),
                        req.question,
                        _image_generation_quality(),
                        _image_generation_size(),
                    )
                    if standalone_images:
                        tools.generated_images.extend(standalone_images)
                        fallback_notes_to_stream.append(
                            _image_generation_note(len(standalone_images))
                        )
                    else:
                        fallback_notes_to_stream.append(
                            _image_generation_failed_note(_image_generation_model())
                        )
                if tools.fact_check:
                    found = check_claim(req.question)
                    if found:
                        tools.fact_checks.extend(found)
                        fallback_notes_to_stream.append(fact_check_note(len(found)))
                if tools.academic_search:
                    papers = search_papers(req.question)
                    if papers:
                        tools.academic_results.extend(papers)
                        fallback_notes_to_stream.append(
                            academic_search_note(len(papers))
                        )
                if tools.self_describe_heuristic or tools.capabilities_calls:
                    fallback_note = _self_describe_note_safely(
                        owner, fallback_model, req, tools.web_search
                    )
                    if fallback_note:
                        fallback_notes_to_stream.append(fallback_note)
                for note in fallback_notes_to_stream:
                    note_text = note if not fallback_parts else f"\n\n{note}"
                    fallback_parts.append(note_text)
                    yield {"event": "delta", "data": {"text": note_text}}

                # See run_orchestrator's twin: a fallback cut off before it
                # wrote anything explains itself, streamed as a delta so a
                # waiting UI shows the reason rather than an empty bubble.
                fallback_no_output = bool(fallback_truncated) and not (
                    "".join(fallback_parts).strip()
                )
                if fallback_no_output:
                    fallback_parts.append(TRUNCATED_EMPTY_ANSWER)
                    yield {
                        "event": "delta",
                        "data": {"text": TRUNCATED_EMPTY_ANSWER},
                    }

                ms = elapsed_ms(meta)

                logger.info(
                    "stream.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_answer = "".join(fallback_parts).strip()
                fallback_notes = (
                    f"{decision.notes} | primary_model={decision.model} failed with "
                    f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                    f"| fallback_reason={REASON_LABELS[reason]} "
                    f"| request_id={meta.request_id} | ms={ms}"
                )
                if image_note:
                    fallback_notes = f"{fallback_notes} | {image_note}"
                # See run_orchestrator's copy: the primary path's own
                # exclusion list, read off THIS call's collectors.
                fallback_extra_cost = (
                    estimate_code_execution_cost(len(tools.code_results)) or 0.0
                ) + (
                    estimate_image_cost(
                        len(tools.generated_images), _image_generation_quality()
                    )
                    or 0.0
                    if tools.generated_images
                    else 0.0
                )
                fallback_cacheable_answer = tools.cacheable(decision.needs_live_data)
                if key is not None and fallback_cacheable_answer:
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        fallback_answer,
                        f"{decision.mode_used}->fallback",
                        fallback_notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                if (
                    context_free
                    and fallback_cacheable_answer
                    and semantic_cache.enabled()
                ):
                    semantic_cache.put(
                        req.question,
                        req.mode.value,
                        owner,
                        semantic_vector,
                        fallback_answer,
                        f"{decision.mode_used}->fallback",
                        fallback_notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    fallback_extra_cost,
                    reservation_id=fallback_reservation_id,
                )
                if fallback_is_free:
                    _record_free_tier_avoided_cost(
                        owner, paid_decision.model, fallback_usage
                    )
                _record_fallback_event(owner, decision.model, reason, succeeded=True)

                yield {
                    "event": "done",
                    "data": {
                        "deployment_id": db_deployment_id(),
                        "answer": fallback_answer,
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": fallback_notes,
                        "model": fallback_model,
                        "truncated": bool(fallback_truncated),
                        "no_output": fallback_no_output,
                        "max_output_tokens": decision.max_output_tokens,
                        **({"sources": tools.citations} if tools.citations else {}),
                        **(
                            {"search_queries": tools.search_queries}
                            if tools.search_queries
                            else {}
                        ),
                        **(
                            {"pending_action": tools.pending_action[0]}
                            if tools.pending_action
                            else {}
                        ),
                        **(
                            {"images": tools.generated_images}
                            if tools.generated_images
                            else {}
                        ),
                        **(
                            {"code_results": tools.code_results}
                            if tools.code_results
                            else {}
                        ),
                        **(
                            {"fact_checks": tools.fact_checks}
                            if tools.fact_checks
                            else {}
                        ),
                        **(
                            {"academic_results": tools.academic_results}
                            if tools.academic_results
                            else {}
                        ),
                        **(
                            {"math_results": tools.math_results}
                            if tools.math_results
                            else {}
                        ),
                        # See run_orchestrator's fallback response: recalled
                        # before the primary call and already in the prompt
                        # this fallback answered from.
                        **(
                            {"library_sources": library_sources}
                            if library_sources
                            else {}
                        ),
                        **(
                            {"memory_sources": memory_sources} if memory_sources else {}
                        ),
                        # Streaming twin of the memorable guard on
                        # run_orchestrator's fallback response — see it, and
                        # AskResponse.memorable. Absent means rememberable, so
                        # this key has to be emitted rather than defaulted.
                        **(
                            {"memorable": False}
                            if (
                                tools.self_describe_heuristic
                                or tools.capabilities_calls
                            )
                            else {}
                        ),
                        **_usage_fields(
                            fallback_model, fallback_usage, fallback_extra_cost
                        ),
                    },
                }
                return

            except GeneratorExit:
                # Same client-disconnect case as the primary stream's handler
                # above, but for a fallback candidate's own stream.
                if fallback_usage.total_tokens:
                    logger.warning(
                        "stream.client_disconnected id=%s fallback_model=%s tokens=%s",
                        meta.request_id,
                        fallback_model,
                        fallback_usage.total_tokens,
                    )
                    _record_spend(
                        owner,
                        fallback_model,
                        fallback_usage,
                        reservation_id=fallback_reservation_id,
                    )
                else:
                    budget.release(fallback_reservation_id)
                raise

            except Exception as fallback_error:
                logger.exception(
                    "stream.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )
                if fallback_is_free:
                    free_tier.exhaust_for_today(fallback_model)

                if fallback_parts:
                    # This fallback streamed partial output; stop entirely.
                    ms = elapsed_ms(meta)
                    yield {
                        "event": "error",
                        "data": {
                            "message": (
                                f"Stream interrupted: {type(fallback_error).__name__}: {fallback_error} "
                                f"| request_id={meta.request_id} | ms={ms}"
                            ),
                        },
                    }
                    return

        ms = elapsed_ms(meta)

        final_reason = (
            BUDGET_REFUSAL if fallbacks and not any_fallback_dispatched else reason
        )
        _record_fallback_event(owner, decision.model, final_reason, succeeded=False)

        if rate_limited:
            message = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| fallback_reason={REASON_LABELS[final_reason]} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            message = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| fallback_reason={REASON_LABELS[final_reason]} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        yield {
            "event": "error",
            "data": {
                "message": message,
                "failure_message": _fallback_exhausted_failure_message(
                    primary_error, ms
                ),
            },
        }
        return
