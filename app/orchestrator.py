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
from collections.abc import Generator
from typing import Any

from dotenv import load_dotenv

from . import budget, cache, database, free_tier, semantic_cache  # noqa: F401 (database re-exported for orchestrator_spend/tests)
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
    looks_like_capabilities_request,
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
    _compose_answer_with_notes,
    _extract_citations,
    _extract_code_results,
    _extract_images,
    _extract_pending_action,
    _extract_search_queries,
    _extract_text,
    _image_generation_note,
    _record_openai_usage,
    _usage_fields,
    _MAX_CITATIONS,
    _MAX_SEARCH_QUERIES,
)
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
    _looks_like_image_request,
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
from .categories import CATEGORY_PROMPT_DEFAULTS
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
# generation's images_wanted (OpenAI tool) vs gemini_image_wanted (Gemini
# phrase heuristic).
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

    Deliberately does NOT touch the tier's token budget or reasoning effort,
    and does not touch prose steps at all — per-step category routing works
    and stays exactly as it is.

    A no-op when CODE_EXECUTION is off (the file simply cannot be produced;
    the step degrades to text, which is the documented behaviour) or when the
    resolved model can already run code.
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
    if capable == decision.model:
        return decision
    return dataclasses.replace(
        decision,
        model=capable,
        notes=(
            f"{decision.notes} | artefact step: moved from "
            f"{decision.model} to {capable} for code execution"
        ),
    )


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

    Silently a no-op (same gating _gate_live_data already applies) unless
    WEB_SEARCH is enabled AND the resolved model is served by a provider with
    a hosted web-search tool wired up (OpenAI or Anthropic) — forcing it
    otherwise would just set a flag nothing downstream acts on.
    """
    if not req.research or decision.needs_live_data:
        return decision
    if (
        not bool_setting("WEB_SEARCH", False)
        or provider_of(decision.model) not in _WEB_SEARCH_PROVIDERS
    ):
        return decision
    return dataclasses.replace(
        decision,
        needs_live_data=True,
        notes=f"{decision.notes} | research mode: forced web search",
    )


def _free_lane_smart_enabled() -> bool:
    return bool_setting("FREE_LANE_SMART", False)


def _tool_flags_for(
    model: str, req: AskRequest, needs_live_data: bool
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool, bool, bool]:
    """(actions_wanted, images_wanted, gemini_image_wanted,
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
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider == "openai"
    )
    gemini_image_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "gemini"
        and _looks_like_image_request(req.question)
    )
    code_execution_wanted = (
        _code_execution_enabled() and provider in _CODE_EXECUTION_PROVIDERS
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
        and looks_like_capabilities_request(req.question)
    )
    any_wanted = (
        needs_live_data
        or actions_wanted
        or images_wanted
        or gemini_image_wanted
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
        gemini_image_wanted,
        code_execution_wanted,
        math_solve_wanted,
        fact_check_wanted,
        academic_search_wanted,
        self_describe_tool_wanted,
        self_describe_heuristic_wanted,
        any_wanted,
    )


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


def run_orchestrator(
    req: AskRequest,
    routing_question: str | None = None,
    owner: str | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
    context_free: bool = False,
    pre_stage_timings: dict[str, int] | None = None,
    library_sources: list[dict[str, Any]] | None = None,
    memory_sources: list[dict[str, Any]] | None = None,
    forced_category: str | None = None,
    allow_auto_workflow: bool = True,
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

    `library_sources` (see app/rag_library.py) is the RAG library's
    `[{"document": ..., "snippet_count": ...}]` summary, computed by the
    caller (routers/messages.py's _recall_library, alongside the same
    library_snippets already folded into `cacheable_system` before this
    call) — this function only threads it onto the response's
    `library_sources` field for transparency, same as `sources`/citations.

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

    decision = _apply_code_execution_override(decision, require_code_execution)
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
        gemini_image_wanted,
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
        _worst_case_image_cost(images_wanted, gemini_image_wanted),
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

    # Automatic, no-toggle-needed image-token cost reduction (downscaling
    # and/or OCR-replacement — see app/image_processing) applied to whatever
    # was attached, once per request; both the primary call and any fallback
    # below reuse the SAME processed attachments/question rather than
    # re-running OCR per candidate model.
    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
    effective_question, cacheable_system = apply_category_role_prompt(
        decision.category, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_concise_mode(
        effective_question, cacheable_system
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

        if gemini_image_wanted:
            gemini_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if gemini_images:
                generated_images.extend(gemini_images)
                answer_text = _compose_answer_with_notes(
                    answer_text, [_image_generation_note(len(gemini_images))]
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
            answer_text = _compose_answer_with_notes(
                answer_text, [self_describe_note(capabilities_snapshot(owner))]
            )

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
            # The pre-dispatch gate ran against the PRIMARY model, whose worst
            # case may have been $0 (a free local Ollama primary that turned
            # out to be down). Re-gate each fallback candidate so the failure
            # of a free model can't route PAID spend past an exhausted cap.
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model, decision.max_output_tokens, req.question, owner=owner
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
                    # Unlike web_search/actions/generated-images (OpenAI/Gemini-
                    # tool-specific, so a fallback provider might not support
                    # them at all), vision/file attachments are threaded to
                    # every provider path — dropping the user's image/document
                    # on fallback would silently lose context they explicitly
                    # provided.
                    attachments=processed_attachments,
                    files=req.files,
                    truncated=fallback_truncated,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                )

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
                fallback_response = AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=fallback_notes,
                    model=fallback_model,
                    truncated=bool(fallback_truncated),
                    **_usage_fields(fallback_model, fallback_usage),
                )
                # Same freshness invariant as the primary path: the fallback
                # never gets web_search/actions/images (documented scope
                # limit), so a live-data question answered by the fallback is
                # not search-grounded and must not be frozen into the cache
                # either. Both backends gated independently — see the
                # primary-path comment above.
                fallback_cacheable_answer = (
                    not decision.needs_live_data
                    and not pending_action
                    and not generated_images
                )
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
    library_sources: list[dict[str, Any]] | None = None,
    memory_sources: list[dict[str, Any]] | None = None,
    forced_category: str | None = None,
    allow_auto_workflow: bool = True,
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

    decision = _apply_code_execution_override(decision, require_code_execution)
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
                "answer": decision.clarifying_question,
                "mode_used": decision.mode_used,
                "notes": f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    (
        actions_wanted,
        images_wanted,
        gemini_image_wanted,
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
        _worst_case_image_cost(images_wanted, gemini_image_wanted),
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

    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
    effective_question, cacheable_system = apply_category_role_prompt(
        decision.category, effective_question, cacheable_system
    )
    effective_question, cacheable_system = apply_concise_mode(
        effective_question, cacheable_system
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

        if gemini_image_wanted:
            gemini_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if gemini_images:
                generated_images.extend(gemini_images)
                note = _image_generation_note(len(gemini_images))
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
            note = self_describe_note(capabilities_snapshot(owner))
            note_text = note if not accumulated else f"\n\n{note}"
            accumulated.append(note_text)
            streamed_any = True
            yield {"event": "delta", "data": {"text": note_text}}

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
                "answer": answer_final,
                "mode_used": decision.mode_used,
                "notes": done_notes,
                "model": decision.model,
                "truncated": bool(truncated),
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
            # Same re-gate as run_orchestrator's fallback loop: the primary's
            # pre-dispatch check may have priced at $0 (free local model), so
            # each paid candidate must clear the budget itself.
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model, decision.max_output_tokens, req.question, owner=owner
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
                    # See run_orchestrator's fallback call: vision/file
                    # attachments work across every provider, unlike the
                    # OpenAI/Gemini-tool extras, so they're kept on fallback too.
                    attachments=processed_attachments,
                    files=req.files,
                    truncated=fallback_truncated,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                ):
                    fallback_parts.append(text)
                    yield {"event": "delta", "data": {"text": text}}

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
                # See run_orchestrator: the fallback never gets web_search,
                # actions, or images, so a live-data question answered by it
                # must not be cached either. Both backends gated independently.
                fallback_cacheable_answer = (
                    not decision.needs_live_data
                    and not pending_action
                    and not generated_images
                )
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
                        "answer": fallback_answer,
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": fallback_notes,
                        "model": fallback_model,
                        "truncated": bool(fallback_truncated),
                        **_usage_fields(fallback_model, fallback_usage),
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
