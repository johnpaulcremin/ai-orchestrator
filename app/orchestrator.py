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

from . import budget, cache, database, semantic_cache  # noqa: F401 (database re-exported for orchestrator_spend/tests)
from .actions import actions_enabled
from .fact_check import (
    check_claim,
    fact_check_enabled,
    format_note as fact_check_note,
    looks_like_fact_check_request,
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
    _extract_text,
    _image_generation_note,
    _record_openai_usage,
    _usage_fields,
    _MAX_CITATIONS,
)
from .orchestrator_spend import _record_avoided_cost, _record_spend
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
from .providers import AUTH_ERRORS, RATE_ERRORS, generate_images_litellm, provider_of
from .routing import _WEB_SEARCH_PROVIDERS, RouteDecision, decide_route
from .schemas import (
    AskRequest,
    AskResponse,
    CodeResult,
    FactCheck,
    MathResult,
    PendingAction,
    Source,
)
from .settings import bool_setting
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


def run_orchestrator(
    req: AskRequest,
    routing_question: str | None = None,
    owner: str | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
    context_free: bool = False,
    pre_stage_timings: dict[str, int] | None = None,
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
    currently just cross-conversation memory's embedding call, timed in
    routers/messages.py before run_orchestrator is ever reached. Purely for
    the per-stage latency breakdown in the `request.ok` log line; never
    affects behavior.
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
        route_question, req.mode, client=client, forced_model=req.model, history=history
    )
    decision = _apply_research_override(decision, req)

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
    # LiteLLM equivalent wired up here for either of those two.
    actions_wanted = (
        actions_enabled() and provider_of(decision.model) in _ACTION_PROVIDERS
    )
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider_of(decision.model) == "openai"
    )
    # The Gemini/Imagen image path is a standalone call, independent of which
    # model answers the question (unlike the OpenAI tool, which only the
    # resolved model itself can decide to invoke) — see _looks_like_image_request.
    gemini_image_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "gemini"
        and _looks_like_image_request(req.question)
    )
    code_execution_wanted = (
        _code_execution_enabled()
        and provider_of(decision.model) in _CODE_EXECUTION_PROVIDERS
    )
    math_solve_wanted = (
        _math_solve_enabled() and provider_of(decision.model) in _MATH_SOLVE_PROVIDERS
    )
    # Same standalone-call design as the Gemini image path: neither OpenAI
    # nor Anthropic offers a hosted fact-check tool, so this is a phrase-
    # heuristic-gated call this app makes itself, independent of which model
    # answers — see fact_check.looks_like_fact_check_request.
    fact_check_wanted = fact_check_enabled() and looks_like_fact_check_request(
        req.question
    )

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
        )
    timer.mark("budget")

    usage = Usage()
    citations: list[Citation] = []
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []
    fact_checks: list[dict[str, object]] = []
    math_results: list[dict[str, object]] = []

    # Automatic, no-toggle-needed image-token cost reduction (downscaling
    # and/or OCR-replacement — see app/image_processing) applied to whatever
    # was attached, once per request; both the primary call and any fallback
    # below reuse the SAME processed attachments/question rather than
    # re-running OCR per candidate model.
    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
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
            notes=notes,
            sources=[Source(**c) for c in citations] or None,
            pending_action=(
                PendingAction.model_validate(pending_action[0])
                if pending_action
                else None
            ),
            images=generated_images or None,
            code_results=[CodeResult.model_validate(c) for c in code_results] or None,
            fact_checks=[FactCheck.model_validate(c) for c in fact_checks] or None,
            math_results=[MathResult.model_validate(m) for m in math_results] or None,
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
            and not math_results
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
        logger.exception(
            "request.primary_model_failed id=%s model=%s err=%s rate_limited=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
            rate_limited,
        )
        # The primary attempt is over (success or fail, no in-between): settle
        # its reservation now rather than carrying it into the fallback loop.
        _record_spend(owner, decision.model, usage, reservation_id=reservation_id)

        fallbacks = _fallback_models(decision.model, cross_provider_only=rate_limited)

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
                    f"| request_id={meta.request_id} | ms={ms}"
                )
                if image_note:
                    fallback_notes = f"{fallback_notes} | {image_note}"
                fallback_response = AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=fallback_notes,
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

        ms = elapsed_ms(meta)

        if rate_limited:
            notes = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            notes = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        return AskResponse(answer="", mode_used=decision.mode_used, notes=notes)


def stream_orchestrator(
    req: AskRequest,
    routing_question: str | None = None,
    owner: str | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
    context_free: bool = False,
    pre_stage_timings: dict[str, int] | None = None,
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
        route_question, req.mode, client=client, forced_model=req.model, history=history
    )
    decision = _apply_research_override(decision, req)

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

    actions_wanted = (
        actions_enabled() and provider_of(decision.model) in _ACTION_PROVIDERS
    )
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider_of(decision.model) == "openai"
    )
    gemini_image_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "gemini"
        and _looks_like_image_request(req.question)
    )
    code_execution_wanted = (
        _code_execution_enabled()
        and provider_of(decision.model) in _CODE_EXECUTION_PROVIDERS
    )
    math_solve_wanted = (
        _math_solve_enabled() and provider_of(decision.model) in _MATH_SOLVE_PROVIDERS
    )
    fact_check_wanted = fact_check_enabled() and looks_like_fact_check_request(
        req.question
    )

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
            "data": {"message": f"{refusal} | request_id={meta.request_id} | ms={ms}"},
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
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []
    fact_checks: list[dict[str, object]] = []
    math_results: list[dict[str, object]] = []

    processed_attachments, ocr_appendix, image_note = process_images(
        req.images, req.question
    )
    effective_question = req.question + ocr_appendix if ocr_appendix else req.question
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
            and not math_results
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

        yield {
            "event": "done",
            "data": {
                "answer": answer_final,
                "mode_used": decision.mode_used,
                "notes": done_notes,
                "truncated": bool(truncated),
                **({"sources": citations} if citations else {}),
                **({"pending_action": pending_action[0]} if pending_action else {}),
                **({"images": generated_images} if generated_images else {}),
                **({"code_results": code_results} if code_results else {}),
                **({"fact_checks": fact_checks} if fact_checks else {}),
                **({"math_results": math_results} if math_results else {}),
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
            "stream.primary_model_failed id=%s model=%s err=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
        )

        for fallback_model in _fallback_models(
            decision.model, cross_provider_only=rate_limited
        ):
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

                yield {
                    "event": "done",
                    "data": {
                        "answer": fallback_answer,
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": fallback_notes,
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

        if rate_limited:
            message = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            message = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        yield {"event": "error", "data": {"message": message}}
        return
