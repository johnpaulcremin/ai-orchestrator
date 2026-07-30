"""This caller's own spend summary — see app/database.py's usage_summary
for the underlying spend_log aggregation shared with the daily budget cap.
"""

from __future__ import annotations

from fastapi import Depends, Query

from .. import feedback
from ..auth import current_owner
from ..budget import daily_budget_per_owner_usd, daily_budget_usd
from ..database import avoided_cost_today, usage_summary
from ..schemas import UsageSummary
from .deps import router


@router.get("/v1/usage", response_model=UsageSummary)
def usage(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own spend: today's total, by-model breakdown, by-day
    series — plus the configured cap(s) and how much of THIS caller's own
    per-owner cap is left today. Never the live global total (see budget.py):
    only the configured limits, which aren't sensitive on their own.
    """
    summary = usage_summary(owner, days)
    owner_limit = daily_budget_per_owner_usd()
    summary["daily_budget_usd"] = daily_budget_usd()
    summary["daily_budget_per_owner_usd"] = owner_limit
    summary["owner_remaining_usd"] = (
        max(0.0, owner_limit - summary["today_usd"])
        if owner_limit is not None
        else None
    )
    summary["avoided_cost_today_usd"] = avoided_cost_today(owner)
    return summary


@router.get("/v1/feedback/summary")
def feedback_summary(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own per-model/per-category/per-lane 👍/👎 aggregates
    over the window (see app/feedback.py) — the quality half of the
    cost-only picture GET /v1/usage gives on its own. Owner-scoped, same as
    every other stat here: never another caller's ratings."""
    return feedback.summarize(owner, days)
