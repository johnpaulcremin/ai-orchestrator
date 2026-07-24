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
prices output plus an approximation of the input prompt, plus the worst-case
cost of an image the call might generate (see would_exceed's `extra_cost_usd`);
an unpriced model can't be bounded on tokens and is logged as a warning, but
IS still bounded on any known image cost (see would_exceed).
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


def would_exceed(
    model: str,
    max_output_tokens: int,
    prompt: str = "",
    extra_cost_usd: float = 0.0,
) -> str | None:
    """A refusal note if dispatching this call would exceed today's budget.

    Returns None when allowed (or no cap is configured). The estimate prices
    the output budget, an approximation of the input prompt, and (via
    `extra_cost_usd`) the worst-case cost of an image the call might generate
    — image generation isn't token-based, so it can't come from the model's
    own price table; callers price it themselves (see orchestrator's
    IMAGE_GENERATION gating) and pass the result in here. Errs toward
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
            return None
        worst = 0.0
    worst += extra_cost_usd
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
