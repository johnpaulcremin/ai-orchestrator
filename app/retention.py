"""Data retention: rollup-before-prune for the ever-growing ledgers, plus the
periodic maintenance pass that actually applies it.

The ledgers this app appends to on every billable call (spend_log,
avoided_cost_log, feedback_log) grow forever by design — nothing before this
module ever deleted a row from them. That's fine for a while, but a
personal, local-first deployment with no operator-managed database
maintenance of its own will eventually notice its SQLite file growing
without bound (the app tripled in size in a day of real use before this
existed). The fix is NOT "just delete old rows": app/database.py's
spend_rollup/avoided_cost_rollup/feedback_rollup tables (see their CREATE
TABLE comments) capture each pruned row's contribution to a monthly, per-
(owner, model) aggregate FIRST, so a chart windowed past the detail
retention boundary still shows the real historical total — see
app/database.py's usage_summary and app/feedback.py's summarize(), which
both read detail ∪ rollup transparently.

No background scheduler, same "no cron-like thread" convention as
db_backup.py/model_catalog.py: maintenance_if_due() is a cheap staleness
check safe to call on every hit of a naturally-frequent, low-stakes request
path, and only actually does the (still fast, but non-trivial) rollup/prune/
optimize/vacuum work on the rare call where a week has passed AND a backup
just completed — chaining onto the backup's own already-established call
site rather than adding a second independent schedule. Deliberately never
called from an ask/ask-stream/regenerate/etc. answering path: those are
latency-sensitive and this work has no business blocking a paid model call.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import database
from .settings import model_setting
from .telemetry import logger

_DEFAULT_RETENTION_DAYS_DETAIL = 365
# Fixed, not operator-configurable (unlike RETENTION_DAYS_DETAIL/
# SHARE_EXPIRY_DAYS) — the spec for this table's own retention is simple
# enough (a per-model daily quota counter, already tiny) that a dedicated
# setting isn't worth the surface area.
FREE_TIER_USAGE_RETENTION_DAYS = 90
_MAINTENANCE_INTERVAL_HOURS = 24 * 7  # weekly
# Only VACUUM when the freelist (pages SQLite could reclaim) is at least
# this fraction of the file — a small freelist isn't worth the exclusive
# lock and I/O a VACUUM takes; "measure first" per the spec.
_VACUUM_MIN_RECLAIMABLE_FRACTION = 0.10
# ...and only bother at all above a floor size, so a fresh/small database
# with a proportionally "large" freelist (a few pages either way) doesn't
# trigger a VACUUM that would reclaim a few KB.
_VACUUM_MIN_REACLAIMABLE_BYTES = 5 * 1024 * 1024


def retention_days_detail() -> int:
    """Days of row-per-call ledger detail to keep before it's rolled up and
    pruned; 0 means keep detail forever (retention disabled). Override >
    env > default, like a model tier — parsed from the same string-valued
    setting FREE_TIER_DEFAULT_QUOTA uses."""
    raw = model_setting("RETENTION_DAYS_DETAIL", str(_DEFAULT_RETENTION_DAYS_DETAIL))
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RETENTION_DAYS_DETAIL
    return value if value >= 0 else _DEFAULT_RETENTION_DAYS_DETAIL


def share_expiry_days() -> int | None:
    """Default share-link expiry in days, or None for "no default expiry"
    (a link lives until revoked — this app's existing behavior, unchanged
    unless an operator opts in). Only ever a DEFAULT: an explicit
    `ttl_hours` on POST .../share always wins over this."""
    raw = model_setting("SHARE_EXPIRY_DAYS", "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def window_start_month(days: int, now: datetime | None = None) -> str:
    """'YYYY-MM' for the first calendar day of a `days`-day window ending
    today (inclusive) — the same window usage_summary/feedback_log_entries
    compute via `date('now', '-N days')`, expressed as a month string so a
    rollup query (monthly granularity) can be bounded to "months this
    window could possibly touch"."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=max(days - 1, 0))
    return f"{start.year:04d}-{start.month:02d}"


def rollup_and_prune(now: datetime | None = None) -> dict[str, int]:
    """Roll every ledger's detail rows older than the configured retention
    window into their monthly aggregates, then delete exactly those rows.
    A no-op for a ledger when RETENTION_DAYS_DETAIL is 0 (keep forever).
    free_tier_usage is pruned on its own fixed 90-day window regardless,
    since it's a compact counter table with nothing to roll up (see
    database.prune_free_tier_usage). Returns a count of rows pruned per
    ledger, for logging/tests.
    """
    now = now or datetime.now(timezone.utc)
    counts = {
        "spend_log": 0,
        "avoided_cost_log": 0,
        "feedback_log": 0,
        "correction_log": 0,
        "fallback_log": 0,
        "free_tier_usage": 0,
    }

    days = retention_days_detail()
    if days > 0:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        counts["spend_log"] = database.rollup_and_prune_spend(cutoff)
        counts["avoided_cost_log"] = database.rollup_and_prune_avoided_cost(cutoff)
        counts["feedback_log"] = database.rollup_and_prune_feedback(cutoff)
        counts["correction_log"] = database.rollup_and_prune_correction(cutoff)
        counts["fallback_log"] = database.rollup_and_prune_fallback(cutoff)

    free_tier_cutoff = (now - timedelta(days=FREE_TIER_USAGE_RETENTION_DAYS)).strftime(
        "%Y-%m-%d"
    )
    counts["free_tier_usage"] = database.prune_free_tier_usage(free_tier_cutoff)

    return counts


def fold_rollup_into_by_model(
    detail_by_model: list[dict[str, Any]],
    owner: str | None,
    window_start_month: str,
) -> list[dict[str, Any]]:
    """Merge spend_rollup's per-model totals (for months >=
    window_start_month) into a usage_summary-style by_model list, adding
    into an existing model row rather than duplicating it. This is what
    makes a usage window that spans the retention boundary still show a
    model's FULL spend for that window, not just whatever's left in detail.
    """
    by_model = {row["model"]: dict(row) for row in detail_by_model}
    for row in database.spend_rollup_by_model(owner, window_start_month):
        model = row["model"]
        existing = by_model.get(model)
        if existing is None:
            by_model[model] = {
                "model": model or "unknown",
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": row["cost_usd"],
            }
            continue
        existing["calls"] += row["calls"]
        existing["input_tokens"] += row["input_tokens"]
        existing["output_tokens"] += row["output_tokens"]
        # None means "no known cost at all" (an unpriced model) in the
        # detail row's own convention — a rolled-up row is only ever
        # written from calls that WERE priced (cost_usd defaults to 0.0,
        # never NULL, in spend_rollup), so it's safe to just add.
        existing["cost_usd"] = (existing["cost_usd"] or 0.0) + row["cost_usd"]
    return sorted(by_model.values(), key=lambda r: r["cost_usd"] or 0.0, reverse=True)


def fold_rollup_into_by_day(
    by_day: list[dict[str, Any]],
    owner: str | None,
    window_start_month: str,
) -> list[dict[str, Any]]:
    """Attribute each rolled-up month's total onto ONE day already present
    in `by_day` (the last day of that month that's actually in the window),
    so a chart spanning the retention boundary still conserves the window's
    real total instead of silently dropping whatever got pruned. This is a
    coarser signal than a real per-day breakdown — rollup has no day-level
    detail to give back — documented here rather than pretended away: a
    month that's been rolled up shows as one lump-sum day, not a smooth
    day-by-day curve.
    """
    if not by_day:
        return by_day
    by_date = {row["date"]: row for row in by_day}
    for row in database.spend_rollup_by_month(owner, window_start_month):
        # Every day in `by_day` whose calendar month matches this rollup
        # row; attribute to the LAST (most recent) one so a partial month at
        # the window's start doesn't put the lump on a day before the
        # window truly begins.
        month_days = sorted(d for d in by_date if d.startswith(row["month"]))
        if not month_days:
            continue
        target = by_date[month_days[-1]]
        target["cost_usd"] += row["cost_usd"]
        target["tokens"] += row["tokens"]
    return by_day


def fold_rollup_into_feedback_by_model(
    by_model: dict[str, dict[str, Any]],
    owner: str | None,
    window_start_month: str,
) -> dict[str, dict[str, Any]]:
    """Same union as fold_rollup_into_by_model, for GET /v1/feedback/
    summary's by_model breakdown — see feedback_rollup's CREATE TABLE
    comment for why this covers by_model only, not by_category/by_lane."""
    merged = {model: dict(stat) for model, stat in by_model.items()}
    for row in database.feedback_rollup_by_model(owner, window_start_month):
        model = row["model"]
        if not model:
            continue
        up, down = row["up_count"], row["down_count"]
        stat = merged.setdefault(
            model, {"answers_rated": 0, "up": 0, "down": 0, "down_rate": 0.0}
        )
        stat["up"] += up
        stat["down"] += down
        stat["answers_rated"] += up + down
        stat["down_rate"] = (
            stat["down"] / stat["answers_rated"] if stat["answers_rated"] else 0.0
        )
    return merged


def fold_rollup_into_correction_by_model(
    by_model: dict[str, dict[str, Any]],
    owner: str | None,
    window_start_month: str,
) -> dict[str, dict[str, Any]]:
    """Merge correction_rollup's per-model flagged counts (for months >=
    window_start_month) into a correction_tracking.summarize()-style
    by_model dict — same union shape as fold_rollup_into_feedback_by_model.
    `answers` needs no folding: it's read straight from `messages`, which
    retention.py never prunes, so it's already correct for the full window;
    only `flagged` (sourced from the now-partially-pruned correction_log)
    needs the rollup's contribution added back in before `correction_rate`
    is recomputed."""
    merged = {model: dict(stat) for model, stat in by_model.items()}
    for row in database.correction_rollup_by_model(owner, window_start_month):
        model = row["model"]
        if not model:
            continue
        stat = merged.setdefault(
            model, {"flagged": 0, "answers": 0, "correction_rate": 0.0}
        )
        stat["flagged"] += row["flagged_count"]
        stat["correction_rate"] = (
            stat["flagged"] / stat["answers"] if stat["answers"] else 0.0
        )
    return merged


def fold_rollup_into_correction_overall(
    overall: dict[str, Any],
    owner: str | None,
    window_start_month: str,
) -> dict[str, Any]:
    """Same fold as fold_rollup_into_correction_by_model, for the single
    "overall" stat — summed across every model's rolled-up flagged count,
    since overall has no model dimension to group by."""
    merged = dict(overall)
    rolled_up_flagged = sum(
        row["flagged_count"]
        for row in database.correction_rollup_by_model(owner, window_start_month)
    )
    merged["flagged"] += rolled_up_flagged
    merged["correction_rate"] = (
        merged["flagged"] / merged["answers"] if merged["answers"] else 0.0
    )
    return merged


def fold_rollup_into_fallback_reasons(
    reasons: list[dict[str, Any]],
    owner: str | None,
    window_start_month: str,
) -> list[dict[str, Any]]:
    """Merge fallback_rollup's per-reason counts (for months >=
    window_start_month) into a database.fallback_reason_counts()-style list,
    re-sorted the same "most common first" way that function itself orders
    by. A complete rollup (see fallback_rollup's CREATE TABLE comment), so
    this fully reconciles regardless of how much detail has been pruned."""
    merged = {row["reason"]: dict(row) for row in reasons}
    for row in database.fallback_rollup_by_reason(owner, window_start_month):
        reason = row["reason"]
        stat = merged.setdefault(reason, {"reason": reason, "count": 0})
        stat["count"] += row["count"]
    return sorted(merged.values(), key=lambda r: (-r["count"], r["reason"]))


def _optimize_and_maybe_vacuum() -> bool:
    """PRAGMA optimize (cheap, SQLite's own "check if any index stats are
    worth refreshing" pass) always; VACUUM only when the reclaimable space
    is both a meaningful fraction of the file AND a meaningful absolute
    size — never on a habitually-tiny local database, and never for a
    freelist that's just normal churn. Returns whether a VACUUM ran."""
    database.optimize()
    reclaimable, total = database.storage_stats()
    vacuumed = False
    if total > 0 and reclaimable >= _VACUUM_MIN_REACLAIMABLE_BYTES:
        fraction = reclaimable / total
        if fraction >= _VACUUM_MIN_RECLAIMABLE_FRACTION:
            database.vacuum()
            vacuumed = True
    return vacuumed


def is_due(now: datetime | None = None) -> bool:
    last = database.last_maintenance_run_at()
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True
    return now - last_dt > timedelta(hours=_MAINTENANCE_INTERVAL_HOURS)


def maintenance_if_due(backup_just_ran: bool) -> dict[str, Any] | None:
    """The actual entry point a request path calls, right after
    db_backup.backup_if_due() — see this module's docstring for why it's
    chained onto the backup call site rather than an independent schedule.
    A no-op (returns None) unless a backup just actually ran AND this
    module's own weekly staleness check says a run is due; never raises —
    a failed maintenance pass must not break whatever request triggered it.
    """
    if not backup_just_ran or not is_due():
        return None
    try:
        counts = rollup_and_prune()
        vacuumed = _optimize_and_maybe_vacuum()
        database.record_maintenance_run()
        result = {**counts, "vacuumed": vacuumed}
        logger.info("db.maintenance %s", result)
        return result
    except (OSError, sqlite3.Error):
        logger.warning("db.maintenance_failed", exc_info=True)
        return None
