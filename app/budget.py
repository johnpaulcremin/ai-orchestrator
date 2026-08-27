"""Spend caps enforced before dispatch: daily totals (global and per-owner)
and a per-answer worst-case ceiling, each refusing a call before a model is
invoked.

The orchestrator measures the USD cost of every answer; this module turns that
into an enforced ceiling. Set DAILY_BUDGET_USD to a positive number and, once
today's total spend (across all users, since UTC midnight) would be exceeded by
the next call, the call is refused before any model is invoked. Unset / 0 /
negative => no global cap (zero overhead: no spend query runs).

MAX_COST_PER_ANSWER_USD is the third, independent axis: not a running total
but a ceiling on any SINGLE call's worst-case estimate, so one enormous
pasted context cannot consume a day's budget in one shot while the daily
total is technically still under its cap. Its refusal note names the figures
(both derive from the caller's own request, unlike the daily caps' totals),
which is what makes it actionable — shorten the prompt, pick a cheaper tier,
or raise the cap. A whole-workflow placeholder reservation is exempt; each
workflow step's own reservation is not (see reserve/reserve_workflow).

DAILY_BUDGET_PER_OWNER_USD is the same idea scoped to one caller's own spend
(see spend_log's `owner` column) instead of everyone's combined total — so one
user maxing out their own budget doesn't also starve everyone else, and vice
versa. Independent of the global cap: either, both, or neither can be
configured, and both are checked when both are set (whichever is hit first
refuses the call). Applies to the owner=None shared bucket too (static-token
or no-auth deployments), where it's simply redundant with the global cap
unless set tighter than it.

Scope: the gate runs on the PRIMARY answer call, on each cross-vendor fallback
candidate before it is dispatched (so a $0-gated primary — e.g. a free local
Ollama model that turns out to be down — cannot route paid fallback spend past
an exhausted cap), and records every answer call's spend. The cheap auxiliary
calls (the gpt-5-nano router classifier and the conversation summarizer) are
neither gated nor counted — so true spend can be slightly above the
recorded/enforced figure. The estimate prices output plus an approximation of
the input prompt, plus the worst-case cost of an image the call might generate
(see reserve's `extra_cost_usd`); an unpriced model can't be bounded on tokens
and is logged as a warning, but IS still bounded on any known image cost. A
call whose worst case is $0 is never refused — free calls can neither consume
the cap nor be blocked by it (see reserve).

Reservation, not check-then-spend: reserve() reads today's spend and inserts a
placeholder spend_log row for this call's worst-case cost in ONE write-locked
transaction (database.try_reserve_spend), so several concurrent calls can't
each read the same stale total and jointly admit past the cap before any of
them has recorded a cent — the gap a plain "read spend, decide, spend later"
check leaves open. Once the call's real outcome is known, the caller
reconciles the reservation via finalize()/release().
"""

from __future__ import annotations

import os

from . import database
from .spend_context import current_conversation_id
from .telemetry import logger
from .usage import Usage, estimate_cost


def daily_budget_usd() -> float | None:
    """The configured global daily cap in USD, or None when disabled."""
    raw = (os.getenv("DAILY_BUDGET_USD") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def daily_budget_per_owner_usd() -> float | None:
    """The configured per-owner daily cap in USD, or None when disabled."""
    raw = (os.getenv("DAILY_BUDGET_PER_OWNER_USD") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def max_cost_per_answer_usd() -> float | None:
    """The configured per-ANSWER worst-case cost ceiling, or None when
    disabled. A third, independent axis from the two daily caps above: those
    bound the day's accumulated total, this bounds any single call — so one
    enormous pasted context can't consume a day's budget in one shot even
    while the daily total is technically still under its cap."""
    raw = (os.getenv("MAX_COST_PER_ANSWER_USD") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# Rough characters-per-token for the pre-dispatch input estimate. English text
# is ~4 chars/token; deliberately not exact — this only needs to be close enough
# to keep the gate from badly under-counting a large context prompt.
_CHARS_PER_TOKEN = 4


def _worst_case_cost(model: str, max_output_tokens: int, prompt: str) -> float | None:
    """Pre-dispatch cost estimate, or None when the model is unpriced.

    Prices the whole output budget PLUS a rough estimate of the input prompt
    (~4 chars/token). In this app `prompt` is often a large assembled context, so
    its input cost can dominate — ignoring it let the gate admit over-limit calls.
    """
    approx_input_tokens = len(prompt) // _CHARS_PER_TOKEN
    return estimate_cost(
        model, Usage(input_tokens=approx_input_tokens, output_tokens=max_output_tokens)
    )


def estimate_worst_case(
    model: str, max_output_tokens: int, prompt: str, extra_cost_usd: float = 0.0
) -> tuple[int, float | None]:
    """Public wrapper around the same (input_tokens_estimate, worst_case_cost)
    reserve() computes internally — for a composer preview to show the user
    BEFORE sending, so the number displayed matches exactly what the budget
    gate itself will check on dispatch, rather than a second, possibly
    inconsistent estimate.

    `extra_cost_usd` is reserve()'s parameter of the same name: the worst-case
    cost of a non-token artefact the call might also produce (an image, a video
    clip), which cannot come from the model's price table because it is not
    billed per token. Without it this wrapper was NOT in fact "the same
    estimate reserve computes" whenever such an artefact was in play — it
    silently dropped the term, and for video, where one clip costs dollars
    against a question's cents, that was most of the number.

    The unpriced-model branch mirrors reserve() deliberately, including the
    part that looks odd: when the TOKEN cost is unknown, a known artefact cost
    is still real money, so it is projected ALONE rather than collapsing the
    whole estimate to None. Returning None there would tell the user "cost
    unknown" about a call whose most expensive component is known exactly.
    """
    approx_input_tokens = len(prompt) // _CHARS_PER_TOKEN
    worst = _worst_case_cost(model, max_output_tokens, prompt)
    if worst is None:
        if extra_cost_usd <= 0:
            return approx_input_tokens, None
        worst = 0.0
    return approx_input_tokens, worst + extra_cost_usd


def reserve(
    model: str,
    max_output_tokens: int,
    prompt: str = "",
    extra_cost_usd: float = 0.0,
    owner: str | None = None,
    per_answer: bool = True,
) -> tuple[str | None, int | None]:
    """Atomically check-and-reserve today's budget for one call.

    Returns (refusal_note, reservation_id). refusal_note is None when the
    call is admitted (or no cap is configured); reservation_id is the
    spend_log row to reconcile via finalize()/release() once the call's real
    outcome is known, or None when nothing needs reconciling (refused, no cap
    configured, or the call's worst case is unpriced/free and so can never
    move the total). The estimate prices the output budget, an approximation
    of the input prompt, and (via `extra_cost_usd`) the worst-case cost of an
    image the call might generate — image generation isn't token-based, so it
    can't come from the model's own price table; callers price it themselves
    (see orchestrator's IMAGE_GENERATION gating) and pass the result in here.
    Errs toward stopping just before the limit rather than just after.

    `per_answer` opts a call out of MAX_COST_PER_ANSWER_USD without touching
    the daily caps. One caller does: reserve_workflow's placeholder, whose
    worst case is deliberately steps × per-step output — an upper bound over
    a whole multi-step workflow that would falsely trip a cap sized for one
    answer. Each of that workflow's real steps still reserves individually
    through here with per_answer=True, so the ceiling holds where it means
    something.
    """
    limit = daily_budget_usd()
    owner_limit = daily_budget_per_owner_usd()
    per_call_limit = max_cost_per_answer_usd() if per_answer else None
    if limit is None and owner_limit is None and per_call_limit is None:
        return None, None
    worst = _worst_case_cost(model, max_output_tokens, prompt)
    if worst is None:
        # The TOKEN cost can't be projected (unpriced model), so it's neither
        # capped nor counted here — warn loudly so a misconfigured/renamed
        # model doesn't silently void the kill-switch. A known image cost is
        # still real money, though, so it alone is still enforced rather than
        # letting the whole call through unbounded.
        logger.warning(
            "budget.unpriced_model model=%s — its token spend is neither "
            "capped nor counted; add it to MODEL_PRICING",
            model,
        )
        if extra_cost_usd <= 0:
            return None, None
        worst = 0.0
    worst += extra_cost_usd
    if worst <= 0:
        # A genuinely free call (e.g. a local Ollama model) can't move the
        # total, so it is never refused — even when recorded spend already
        # sits past the cap (reachable via fallback overshoot or concurrent
        # admits). The strict check below would otherwise brick the free tier
        # for the rest of the UTC day over spend it didn't cause.
        return None, None
    if per_call_limit is not None and worst > per_call_limit:
        logger.warning(
            "budget.per_answer_refused cap=%.4f worst_case=%.4f model=%s owner=%s",
            per_call_limit,
            worst,
            model,
            owner,
        )
        # Unlike the daily-cap note below, this one names its figures: both
        # derive from the caller's OWN request (its size, the model's price),
        # not from anyone else's spend, and the number is what makes the
        # refusal actionable — shorten the prompt, pick a cheaper tier, or
        # raise the cap.
        return (
            f"This request's estimated worst-case cost (~${worst:.2f}) exceeds "
            f"the per-answer cap (${per_call_limit:.2f}). Try a cheaper "
            "tier/model or a shorter prompt, or raise MAX_COST_PER_ANSWER_USD.",
            None,
        )
    if limit is None and owner_limit is None:
        # Only the per-answer ceiling is configured and this call passed it —
        # there is no daily total to reserve against, so nothing to reconcile.
        return None, None
    try:
        admitted, spent, reservation_id = database.try_reserve_spend(
            owner, model, worst, limit, owner_limit, current_conversation_id()
        )
    except Exception:
        # Fail open: a transient DB error must not hard-fail requests — the
        # cap resumes on the next call. The operator still sees it in the logs.
        logger.exception("budget.reserve_failed model=%s", model)
        return None, None
    if not admitted:
        logger.warning(
            "budget.refused limit=%s owner_limit=%s spent=%.4f worst_case=%.4f "
            "model=%s owner=%s",
            f"{limit:.4f}" if limit is not None else "n/a",
            f"{owner_limit:.4f}" if owner_limit is not None else "n/a",
            spent,
            worst,
            model,
            owner,
        )
        # Generic note: don't disclose which cap (global vs per-owner) or the
        # actual figures to the caller — the specifics are in the log line above.
        return "Daily budget reached. Request refused; it resets at 00:00 UTC.", None
    return None, reservation_id


def reserve_workflow(
    step_model: str,
    max_output_tokens_per_step: int,
    step_count: int,
    prompt: str = "",
    owner: str | None = None,
) -> tuple[str | None, int | None]:
    """Reserve the worst case for an ENTIRE opt-in workflow (see
    app/workflow.py) as ONE atomic reservation up front — steps × per-step
    output cap — rather than reserving per-step, so several concurrent
    workflows can't each read the same stale total and jointly admit past
    the cap (the exact race reserve()'s docstring describes, just at
    workflow granularity instead of single-call granularity).

    `step_model` should be the priciest model any step could plausibly
    resolve to — the smart-tier model is the natural, conservative choice,
    since a step's category could in principle route all the way up to
    smart. This is a worst-case UPPER BOUND placeholder, not a running
    total: each step still executes through the ordinary single-ask
    pipeline (run_orchestrator/stream_orchestrator), which reserves and
    finalizes its OWN much-smaller real cost via the same reserve()/
    finalize_spend() machinery every other call uses. Once every step (and
    the synthesis step) has completed, the caller releases THIS placeholder
    (see release()) — the real per-step spend is already recorded
    individually, so releasing the placeholder simply returns the unused
    slack between "worst case reserved up front" and "what actually got
    spent" back to today's available budget, exactly the "reconcile down on
    completion" the workflow spec calls for.
    """
    return reserve(
        step_model,
        max_output_tokens_per_step * max(step_count, 1),
        prompt,
        owner=owner,
        # A whole-workflow upper bound is not one answer; the per-answer cap
        # applies to each step's own reservation instead (see reserve).
        per_answer=False,
    )


def release(reservation_id: int | None) -> None:
    """Release a reservation whose call never produced any billable usage
    (errored before dispatch reached the provider, or a fallback candidate
    that itself failed) so it stops counting against today's cap. A no-op
    when reservation_id is None — nothing was reserved for that call."""
    if reservation_id is None:
        return
    try:
        database.release_spend(reservation_id)
    except Exception:
        logger.exception("budget.release_failed reservation_id=%s", reservation_id)


def budget_status() -> dict[str, object]:
    """Budget block for the public, unauthenticated /v1/status.

    Reports ONLY whether each cap is configured, not the figures themselves.
    The live limits / spend / remaining are deliberately withheld here so an
    anonymous caller can't read the deployment's daily spend; the operator
    reads those from logs (or a future authenticated endpoint).
    """
    return {
        "enabled": daily_budget_usd() is not None,
        "per_owner_enabled": daily_budget_per_owner_usd() is not None,
        "per_answer_enabled": max_cost_per_answer_usd() is not None,
    }
