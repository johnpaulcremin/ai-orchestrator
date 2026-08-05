"""This caller's own spend summary — see app/database.py's usage_summary
for the underlying spend_log aggregation shared with the daily budget cap.
"""

from __future__ import annotations

from fastapi import Depends, Query

from .. import correction_tracking, feedback, retention, self_report
from ..auth import current_owner
from ..budget import daily_budget_per_owner_usd, daily_budget_usd
from ..database import (
    avoided_cost_today,
    fallback_reason_counts,
    last_self_report_run_at,
    usage_summary,
)
from ..schemas import UsageSummary
from ..settings import bool_setting
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

    by_model/by_day are unioned with app/retention.py's monthly rollups, so
    a window that spans the RETENTION_DAYS_DETAIL boundary still reflects
    the real historical total rather than silently dropping whatever's been
    pruned from spend_log — a no-op merge when nothing has been pruned yet
    (the common case, since rollup queries return no rows).
    """
    summary = usage_summary(owner, days)
    start_month = retention.window_start_month(days)
    summary["by_model"] = retention.fold_rollup_into_by_model(
        summary["by_model"], owner, start_month
    )
    summary["by_day"] = retention.fold_rollup_into_by_day(
        summary["by_day"], owner, start_month
    )
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
    every other stat here: never another caller's ratings.

    by_model is additionally unioned with feedback_rollup (see
    app/retention.py) for continuity across the retention boundary;
    by_category/by_lane are not (see feedback_rollup's CREATE TABLE
    comment for why that finer detail doesn't survive a prune).
    """
    summary = feedback.summarize(owner, days)
    summary["by_model"] = retention.fold_rollup_into_feedback_by_model(
        summary["by_model"], owner, retention.window_start_month(days)
    )
    return summary


@router.get("/v1/correction/summary")
def correction_summary(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own implicit-correction tally (see
    app/correction_tracking.py) — overall, and per-model/per-category/
    per-lane, same dimensions as GET /v1/feedback/summary. A NOISY PROXY,
    not a verified error rate — see that module's docstring. Lets a caller
    check the current correction rate on demand, not only in the weekly
    System report.

    `overall`/`by_model` are additionally unioned with correction_rollup
    (see app/retention.py) for continuity across the retention boundary;
    `by_category`/`by_lane` are not (coarser rollup, same tradeoff
    feedback_rollup already makes).
    """
    summary = correction_tracking.summarize(owner, days)
    start_month = retention.window_start_month(days)
    summary["by_model"] = retention.fold_rollup_into_correction_by_model(
        summary["by_model"], owner, start_month
    )
    summary["overall"] = retention.fold_rollup_into_correction_overall(
        summary["overall"], owner, start_month
    )
    return summary


@router.get("/v1/fallback/summary")
def fallback_summary(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own paid-fallback-cause tally (see
    app/fallback_reason.py) — the same "why did the router fall back" data
    the weekly System report's "Paid fallback causes" section tallies, one
    click away instead of waiting for the next report. A complete rollup
    (see fallback_rollup's CREATE TABLE comment), so this reconciles in full
    across the retention boundary.
    """
    return {
        "reasons": retention.fold_rollup_into_fallback_reasons(
            fallback_reason_counts(owner, days),
            owner,
            retention.window_start_month(days),
        )
    }


@router.get("/v1/self-report/status")
def self_report_status(owner: str | None = Depends(current_owner)):
    """This owner's weekly self-report status (see app/self_report.py) —
    when their last one was generated, and whether SELF_REPORT_NARRATE is
    currently on — for the Usage panel's "Generate now" section."""
    return {
        "last_generated_at": last_self_report_run_at(owner),
        "narrate_enabled": bool_setting("SELF_REPORT_NARRATE", False),
    }


@router.post("/v1/self-report/generate")
def self_report_generate_now(owner: str | None = Depends(current_owner)):
    """Force-generate this owner's weekly self-report right now, ignoring
    the usual staleness check — the "Generate now" button. Lands as a
    normal owner-scoped conversation, same as the automatic weekly one."""
    return self_report.generate_report(owner)
