"""Cache effectiveness over a window: how often an answer was served without
a model call, and what that avoided.

Its own module because two places report it and they must not drift. The
weekly self-report (app/self_report.py) has shown a cache-hit rate since it
was written; the Usage panel never did, and a model asked to critique this
app duly reported that it had no visibility into cache behaviour and
proposed building the very figure the weekly digest already prints. The fix
is one computation both callers share, not a second implementation that
agrees with the first until someone edits one of them.

The denominator is the interesting decision. `total_requests` counts every
request that resolved into an ANSWER, by whichever path: a real model call
(including a $0 free-lane one) or a cache hit that served an answer with no
call at all. It is deliberately not "rows in spend_log" — a cache hit writes
no spend row, so dividing by that would compute a hit rate over only the
requests that missed the cache, which rises toward 100% exactly as caching
stops working.

`by_model` is passed in rather than queried here so each caller measures the
same numbers it displays — both callers have already folded app/retention.py's
monthly rollups into theirs, and a window spanning the retention boundary
would otherwise divide freshly-counted hits by a pruned call count.
"""

from __future__ import annotations

from typing import Any

from . import database


def summarize(
    owner: str | None, days: int, by_model: list[dict[str, Any]]
) -> dict[str, Any]:
    """{"total_requests", "exact_hits", "semantic_hits", "exact_hit_rate",
    "semantic_hit_rate", "avoided_cost_usd"} for this owner over `days`.

    Both rates are None (not 0.0) when the window holds no requests at all —
    the same "no data" vs "measured zero" distinction usage_summary draws
    with tokens_per_dollar, and the reason the frontend can render "—"
    instead of a 0% that would read as a broken cache.
    """
    by_reason = database.avoided_cost_by_reason(owner, days)
    exact = int(by_reason.get("response_cache_hit", {}).get("count", 0))
    semantic = int(by_reason.get("semantic_cache_hit", {}).get("count", 0))
    real_calls = sum(int(row["calls"]) for row in by_model)
    total = real_calls + exact + semantic
    return {
        "total_requests": total,
        "exact_hits": exact,
        "semantic_hits": semantic,
        "exact_hit_rate": (exact / total) if total else None,
        "semantic_hit_rate": (semantic / total) if total else None,
        "avoided_cost_usd": sum(
            float(row["avoided_cost_usd"]) for row in by_reason.values()
        ),
    }
