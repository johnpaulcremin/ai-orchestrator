"""Global daily spend cap — a kill-switch for AI cost.

The orchestrator measures the USD cost of every answer; this module turns that
into an enforced ceiling. Set DAILY_BUDGET_USD to a positive number and, once
today's total spend (across all users, since UTC midnight) would be exceeded by
the next call, the call is refused before any model is invoked. Unset / 0 /
negative => no cap (zero overhead: no spend query runs).

This is the global slice; a per-owner daily cap is a later addition on the same
spend_log data layer.
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


def _worst_case_cost(model: str, max_output_tokens: int) -> float:
    """Conservative pre-dispatch estimate: the whole output budget at the model's
    output rate (input tokens aren't known until the call runs). 0.0 if unpriced.
    """
    return estimate_cost(model, Usage(output_tokens=max_output_tokens)) or 0.0


def would_exceed(model: str, max_output_tokens: int) -> str | None:
    """A refusal note if dispatching this call would exceed today's budget.

    Returns None when allowed (or no cap is configured). The check is worst-case
    on output cost, so it errs toward stopping just before the limit rather than
    just after.
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
    worst = _worst_case_cost(model, max_output_tokens)
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
