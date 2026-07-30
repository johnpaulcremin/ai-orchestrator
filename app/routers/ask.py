"""Stateless ask endpoints: /v1/ask, /v1/compare, /v1/estimate. None of
these build or persist any conversation context — see app/routers/messages.py
for the conversation-scoped ask/regenerate/edit/continue endpoints.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, Request

from ..auth import current_owner
from ..budget import estimate_worst_case
from ..orchestrator import run_orchestrator
from ..routing import decide_route
from ..ratelimit import limiter, rate_limit_value
from ..settings import get_model_overrides, model_setting
from ..workflow import max_steps as workflow_max_steps
from ..workflow import step_max_output_tokens as workflow_step_max_output_tokens
from ..schemas import (
    AskRequest,
    AskResponse,
    CompareRequest,
    CompareResponse,
    CompareResult,
    EstimateRequest,
    EstimateResponse,
    Mode,
)
from ..telemetry import elapsed_ms, new_request_meta
from .deps import router


@router.post("/v1/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask(
    request: Request,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    # Stateless: no conversation history or system prompt is ever built here,
    # so this is always the context-free shape the semantic cache requires.
    return run_orchestrator(req, owner=owner, context_free=True)


@router.post("/v1/compare", response_model=CompareResponse)
@limiter.limit(rate_limit_value)
def compare(
    request: Request,
    req: CompareRequest,
    owner: str | None = Depends(current_owner),
):
    """Ask the same question of 2-4 specific models and report each answer
    alongside its cost/tokens/latency — a direct way to see what
    multi-provider routing actually trades off.

    Dispatched to every model CONCURRENTLY via a thread pool sized to the
    request (2-4 models, so at most 4 threads — matches CompareRequest's own
    cap). Safe despite the shared daily budget: budget.reserve()'s
    underlying SQLite reservation (`BEGIN IMMEDIATE`, see
    database.try_reserve_spend) already serializes concurrent spend checks
    correctly — that atomicity is what lets ordinary concurrent requests to
    this whole app coexist safely in the first place, so a handful of
    threads from one /v1/compare call needs nothing extra. Each dispatch is
    otherwise independent (its own AskRequest, its own local `meta`).
    executor.map preserves request order in its results regardless of which
    model actually finishes first. Matches run_orchestrator's own guarantee
    — it never raises for an ordinary provider failure, only reports an
    empty answer + explanatory notes — so one model being
    unconfigured/failing never aborts the rest of the comparison.
    """

    def _dispatch(model: str) -> CompareResult:
        meta = new_request_meta()
        response = run_orchestrator(
            AskRequest(question=req.question, model=model),
            owner=owner,
            context_free=True,
        )
        return CompareResult(
            model=model,
            answer=response.answer,
            mode_used=response.mode_used,
            notes=response.notes,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            elapsed_ms=elapsed_ms(meta),
        )

    with ThreadPoolExecutor(max_workers=len(req.models)) as executor:
        results = list(executor.map(_dispatch, req.models))

    return CompareResponse(question=req.question, results=results)


@router.post("/v1/estimate", response_model=EstimateResponse)
@limiter.limit(rate_limit_value)
def estimate(
    request: Request,
    req: EstimateRequest,
    owner: str | None = Depends(current_owner),
):
    """What would this question cost if sent, without sending it — for a
    live composer preview as the user types.

    Routes with `client=None`, which makes decide_route skip the AI
    classifier entirely (even in auto mode, falling back to the free keyword
    heuristic) — a preview must never itself spend a classifier call. The
    token/cost figures come from budget.estimate_worst_case, the exact same
    worst-case estimate the real DAILY_BUDGET_USD gate uses on dispatch, so
    what's previewed here matches what would actually be checked/billed
    (mode's max_output_tokens as the worst-case output, ~4 chars/token for
    input) rather than a second, possibly-inconsistent guess.

    mode="workflow" is a special case: the real plan (and so the real step
    count) isn't known without a planning call, which — same as the
    classifier above — a preview must never spend. Instead this previews the
    same worst-case budget.reserve_workflow() itself reserves up front: the
    smart-tier model, priced for max_steps()+1 calls (every planned step
    plus the synthesis step) at step_max_output_tokens() each — the actual
    "up to ~$X" ceiling a workflow ask can spend, not a guess at what the
    plan will contain.
    """
    if req.mode == Mode.workflow:
        overrides = get_model_overrides()
        base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
        smart_model = model_setting("OPENAI_MODEL_SMART", base, overrides)
        step_count = workflow_max_steps() + 1
        input_tokens_estimate, cost_usd_estimate = estimate_worst_case(
            smart_model, workflow_step_max_output_tokens() * step_count, req.question
        )
        return EstimateResponse(
            model=smart_model,
            mode_used=f"workflow(up to {workflow_max_steps()} steps)",
            input_tokens_estimate=input_tokens_estimate,
            output_tokens_estimate=workflow_step_max_output_tokens() * step_count,
            cost_usd_estimate=cost_usd_estimate,
        )

    decision = decide_route(req.question, req.mode, client=None)
    input_tokens_estimate, cost_usd_estimate = estimate_worst_case(
        decision.model, decision.max_output_tokens, req.question
    )
    return EstimateResponse(
        model=decision.model,
        mode_used=decision.mode_used,
        input_tokens_estimate=input_tokens_estimate,
        output_tokens_estimate=decision.max_output_tokens,
        cost_usd_estimate=cost_usd_estimate,
    )
