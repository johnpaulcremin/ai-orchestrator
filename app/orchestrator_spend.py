"""Best-effort spend and avoided-cost logging for a completed orchestrator call.
Both functions never raise — a logging failure must never break the answer
already produced."""

from __future__ import annotations

from . import budget, database
from .telemetry import logger
from .usage import Usage, estimate_cost


def _record_spend(
    owner: str | None,
    model: str,
    usage: Usage,
    extra_cost_usd: float = 0.0,
    reservation_id: int | None = None,
) -> None:
    """Best-effort spend-log write for a completed call (never breaks the answer).

    Recorded even when the answer is empty/truncated, as long as tokens were
    spent, so the daily budget accounts for calls not persisted as messages.
    `extra_cost_usd` folds in non-token costs (currently: generated images).

    `reservation_id` (from budget.reserve()) is the pre-dispatch placeholder
    row to reconcile rather than inserting a fresh one — see try_reserve_spend
    in database.py. A call with no usage and no extra cost genuinely spent
    nothing, so any held reservation is released instead of finalized.
    """
    if not usage.total_tokens and not extra_cost_usd:
        budget.release(reservation_id)
        return
    try:
        cost = estimate_cost(model, usage)
        if extra_cost_usd:
            # Only force None (unpriced) to 0.0 when there's an extra cost to
            # fold in — an unpriced model with no extra cost stays None
            # (unknown), never a misleading "free".
            cost = (cost or 0.0) + extra_cost_usd
        if reservation_id is not None:
            database.finalize_spend(
                reservation_id, usage.input_tokens, usage.output_tokens, cost
            )
        else:
            database.record_spend(
                owner, model, usage.input_tokens, usage.output_tokens, cost
            )
    except Exception:
        logger.exception("spend.record_failed model=%s", model)


def _record_avoided_cost(owner: str | None, hit: dict, reason: str) -> None:
    """Best-effort avoided-cost-log write for a response-cache hit (never
    breaks the answer). `hit` is the cache entry itself — its own `cost_usd`
    is what THIS call would have cost had it gone live instead of being
    served from cache, so that's exactly what gets logged as avoided.
    """
    avoided_cost = hit.get("cost_usd")
    if not isinstance(avoided_cost, (int, float)):
        avoided_cost = None
    try:
        database.record_avoided_cost(owner, hit.get("model"), reason, avoided_cost)
    except Exception:
        logger.exception("avoided_cost.record_failed reason=%s", reason)
