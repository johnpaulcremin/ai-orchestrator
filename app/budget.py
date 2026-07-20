"""Global daily spend cap — a kill-switch for AI cost.

The orchestrator measures the USD cost of every answer; this module turns that
into an enforced ceiling. Set DAILY_BUDGET_USD to a positive number and, once
today's total spend (across all users, since UTC midnight) would be exceeded by
the next call, the call is refused before any model is invoked. Unset / 0 /
negative => no cap (zero overhead: no spend query runs).

This is the global slice; a per-owner daily cap is a later addition on the same
spend_log data layer.

Scope (intentional): the gate runs on the PRIMARY answer call and records every
answer call's spend. It does not separately gate the exceptional cross-vendor
fallback dispatch, and the cheap auxiliary calls (the gpt-5-nano router
classifier and the conversation summarizer) are neither gated nor counted — so
true spend can be slightly above the recorded/enforced figure. The estimate
prices output plus an approximation of the input prompt; an unpriced model can't
be bounded and is logged as a warning (see would_exceed).
"""

from __future__ import annotations

import os

from . import database
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


def would_exceed(model: str, max_output_tokens: int, prompt: str = "") -> str | None:
    """A refusal note if dispatching this call would exceed today's budget.

    Returns None when allowed (or no cap is configured). The estimate prices both
    the output budget and an approximation of the input prompt, so it errs toward
    stopping just before the limit rather than just after.
    """
    limit = daily_budget_usd()
    if limit is None:
        return None
    try:
        spent = database.spend_today_usd()
    except Exception:
        # Fail open: a transient DB read error must not hard-fail requests — the
        # cap resumes on the next call. The operator still sees it in the logs.
        logger.exception("budget.spend_read_failed model=%s", model)
        return None
    worst = _worst_case_cost(model, max_output_tokens, prompt)
    if worst is None:
        # The model isn't in the price table, so its spend can be neither
        # projected here nor summed into the running total — the cap cannot bound
        # it. Warn loudly so a misconfigured/renamed model doesn't silently void
        # the kill-switch, and let the call through (fail open).
        logger.warning(
            "budget.unpriced_model model=%s — its spend is neither capped nor "
            "counted; add it to MODEL_PRICING",
            model,
        )
        return None
    if spent + worst > limit:
        logger.warning(
            "budget.refused limit=%.4f spent=%.4f worst_case=%.4f model=%s",
            limit,
            spent,
            worst,
            model,
        )
        # Generic note: don't disclose the limit or global spend to the caller
        # (the specifics are in the log line above).
        return "Daily budget reached. Request refused; it resets at 00:00 UTC."
    return None


def budget_status() -> dict[str, object]:
    """Budget block for the public, unauthenticated /v1/status.

    Reports ONLY whether a cap is configured. The live limit / spend / remaining
    are deliberately withheld here so an anonymous caller can't read the
    deployment's daily spend; the operator reads those from logs (or a future
    authenticated endpoint).
    """
    return {"enabled": daily_budget_usd() is not None}
