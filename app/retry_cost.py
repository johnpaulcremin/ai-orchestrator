"""Re-run cost: what a routing decision REALLY cost, retries included.

The read half of app/retry_attribution.py (see that module for why a retry
destroys its own evidence, and why the signal is not one counter). This one
answers, per category and per tier:

  - first-attempt cost — what the ledgers used to show, and what the router
    optimises for;
  - total cost — the same turns including every retry of them, which is what
    the money actually was;
  - retry rate — how often a turn needed a second go at all.

A cheap answer regenerated twice cost more than a dearer one that landed
first time. Judged on first-attempt cost, the cheap route wins; judged on
total cost, it may not. That comparison is the only thing this module is for.
It changes no routing behaviour and is read by no router.

ATTRIBUTION IS BACK TO THE ORIGINAL DECISION, always. Every retry's cost, and
every implicit correction, is booked against the routing decision of the
turn's FIRST attempt, not the attempt that happened to be live when it
occurred. This matters more than it sounds: a regenerate re-routes (it honours
req.mode/req.model afresh), so a turn that started cheap and was retried on a
dearer model would otherwise book the whole overrun against the dear model
that cleaned up the mess — exactly backwards, and it would read as evidence
for routing MORE traffic to the cheap first choice.

WHAT THESE NUMBERS CAN AND CANNOT SUPPORT — the same treatment the routing
eval's ceilings got (see evals/separability.py), for the same reason: a rate
printed bare invites a conclusion its sample size cannot carry. On a personal
deployment with tens of conversations and a handful of regenerations, "40%
retry rate" is five samples and two events. So:

  - Every rate is reported with its n, and never without it.
  - Every rate carries a 95% Wilson score interval. At 2/5 that interval is
    roughly 12%-78%, which contains both "this route is fine" and "this route
    is failing" — the number cannot distinguish them, and says so.
  - `reads_as` classifies each rate: "no_data", "insufficient" (the interval
    is wider than +/-10 points, so the figure is a direction at best), or
    "directional". `turns_for_directional` says how many turns at that same
    observed rate it would take to get inside +/-10 points, which is the
    honest answer to "when will this mean something".
  - The +/-10-point line is a PRESENTATION guardrail, not a statistical test.
    It exists so a five-sample 40% cannot render like a finding; it is not a
    significance threshold and nothing downstream branches on it.

FURTHER LIMITS, which no amount of data fixes:
  - Retries predating retry_log are invisible; a turn's first RECORDED
    attempt is treated as its original.
  - A failed retry (empty answer) replaced nothing, so it is not counted here,
    though it may have cost money — that spend is in spend_log and remains
    unattributable, as it always was.
  - cost_usd is NULL for a genuinely unpriced model and 0.0 for a real
    free-lane answer. Both sum as 0; `unpriced_attempts` counts the former so
    a $0 total is not mistaken for a free one.
  - The retry-rate denominator is answers, one per turn. It excludes this
    app's own generated 📊 System report messages (mode_used="self_report"),
    which are not routed turns and can never be retried — counting them would
    dilute every rate by however many reports exist.
"""

from __future__ import annotations

import math
from typing import Any

from . import database
from .feedback import lane_from_mode_used, parse_mode_used
from .retry_attribution import SIGNALS

# 95%, two-sided. Hard-coded rather than configurable: this is a presentation
# convention, and an interval whose confidence level moved between readings
# would be worse than no interval.
_Z = 1.959963984540054
# A rate is only worth reading as a direction when its 95% interval is no
# wider than this — i.e. roughly +/-10 points. See the module docstring: a
# guardrail on rendering, not a significance test.
_DIRECTIONAL_WIDTH = 0.20
# Cap on the turns_for_directional search. Past this the answer is "far more
# than this deployment will produce", and a precise figure would be false
# comfort about how reachable it is.
_MAX_PROJECTED_TURNS = 100_000

# Not a routed turn: this app's own weekly report, written straight to a
# conversation by app/self_report.py. It has no routing decision, no cost, and
# no retry is possible on it.
_NON_TURN_MODE = "self_report"


def wilson_interval(successes: int, total: int) -> tuple[float, float] | None:
    """95% Wilson score interval for `successes`/`total`, or None when there
    is nothing to bound (total 0).

    Wilson rather than the textbook normal approximation because every
    interesting case here is a small n with a proportion near 0 or 1, which is
    exactly where the normal approximation returns nonsense (0/5 -> +/-0, a
    perfect-looking certainty from five samples). Wilson stays inside [0, 1]
    and never collapses to a point. Computed here in a few lines rather than
    pulling in a stats dependency for one formula.
    """
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + _Z**2 / total
    centre = (proportion + _Z**2 / (2 * total)) / denominator
    spread = (
        _Z
        / denominator
        * math.sqrt(proportion * (1 - proportion) / total + _Z**2 / (4 * total**2))
    )
    return max(0.0, centre - spread), min(1.0, centre + spread)


def turns_for_directional(successes: int, total: int) -> int | None:
    """How many turns it would take, at the SAME observed rate, for the 95%
    interval to fit inside _DIRECTIONAL_WIDTH — the concrete form of "this
    number cannot support a conclusion yet".

    None when there is no rate to project from (total 0) or when the answer
    exceeds _MAX_PROJECTED_TURNS. Returns `total` itself when the current
    sample already qualifies.
    """
    if total <= 0:
        return None
    proportion = successes / total
    projected = total
    while projected <= _MAX_PROJECTED_TURNS:
        interval = wilson_interval(round(proportion * projected), projected)
        if interval is not None and interval[1] - interval[0] <= _DIRECTIONAL_WIDTH:
            return projected
        projected += max(1, projected // 20)
    return None


def _empty_stat() -> dict[str, Any]:
    return {
        "turns": 0,
        "retried_turns": 0,
        "retries": 0,
        "corrections": 0,
        "unpriced_attempts": 0,
        "first_attempt_cost_usd": 0.0,
        "total_cost_usd": 0.0,
    }


def _finalize(stat: dict[str, Any]) -> dict[str, Any]:
    """Add the derived figures — the rate, its interval, its sufficiency
    verdict, and the cost multiplier — once a stat's counters are final."""
    turns = int(stat["turns"])
    retried = int(stat["retried_turns"])
    interval = wilson_interval(retried, turns)
    stat["retry_rate"] = retried / turns if turns else 0.0
    stat["retry_rate_ci"] = list(interval) if interval else None
    stat["turns_for_directional"] = turns_for_directional(retried, turns)
    if not turns:
        stat["reads_as"] = "no_data"
    elif interval is not None and interval[1] - interval[0] <= _DIRECTIONAL_WIDTH:
        stat["reads_as"] = "directional"
    else:
        stat["reads_as"] = "insufficient"
    first = float(stat["first_attempt_cost_usd"])
    stat["retry_cost_usd"] = float(stat["total_cost_usd"]) - first
    stat["cost_multiplier"] = (
        float(stat["total_cost_usd"]) / first if first > 0 else None
    )
    return stat


def _bump(
    buckets: list[dict[str, dict[str, Any]]],
    keys: list[str | None],
    field: str,
    amount: float = 1,
) -> None:
    """Add `amount` to `field` on each bucket whose key resolves. A dimension
    that cannot be resolved (no category in mode_used, no recognisable lane)
    is simply absent from that one breakdown — same convention as
    app/feedback.py and app/correction_tracking.py, so a row still counts in
    whichever breakdowns it DOES resolve for, and in `overall` regardless.
    """
    for bucket, key in zip(buckets, keys, strict=True):
        if key is None:
            continue
        stat = bucket.setdefault(key, _empty_stat())
        stat[field] = stat[field] + amount


def _turn_dimensions(mode_used: str | None) -> list[str | None]:
    """(category, tier) for one attempt's routing decision, parsed exactly the
    way feedback_log/correction_log parse theirs — so a category or a lane
    means the same thing in every one of this app's quality ledgers."""
    return [parse_mode_used(mode_used)[1], lane_from_mode_used(mode_used)]


def summarize(owner: str | None, days: int) -> dict[str, Any]:
    """First-attempt cost, total cost, retry rate and correction count per
    category and per tier over the window, plus the signal split that keeps
    "regenerated" from being read as one thing.

    Two sources, deliberately: retry_log owns every turn that WAS retried
    (whole attempt chains, costs included, since the earlier attempts no
    longer exist anywhere else), and `messages` supplies the turns that were
    not — skipped by message id wherever retry_log already accounts for them,
    so no answer is counted twice and no retried turn is booked at its last
    attempt's route.
    """
    retry_rows = database.retry_log_turn_rows(owner, days)

    chains: dict[int, list[dict[str, Any]]] = {}
    for row in retry_rows:
        chains.setdefault(int(row["turn_key"]), []).append(row)

    overall = _empty_stat()
    by_category: dict[str, dict[str, Any]] = {}
    by_tier: dict[str, dict[str, Any]] = {}
    by_signal: dict[str, dict[str, Any]] = {
        signal: {"retries": 0, "retry_cost_usd": 0.0} for signal in SIGNALS
    }
    buckets = [by_category, by_tier]

    # message_id -> the ORIGINAL attempt's dimensions, for re-attributing a
    # correction flag raised against a later attempt (and for skipping every
    # attempt of a retried turn when `messages` is walked below).
    origin_by_message: dict[int, list[str | None]] = {}

    for attempts in chains.values():
        attempts.sort(key=lambda row: int(row["attempt_index"]))
        original = attempts[0]
        keys = _turn_dimensions(original["mode_used"])

        overall["turns"] += 1
        _bump(buckets, keys, "turns")
        if len(attempts) > 1:
            overall["retried_turns"] += 1
            overall["retries"] += len(attempts) - 1
            _bump(buckets, keys, "retried_turns")
            _bump(buckets, keys, "retries", len(attempts) - 1)

        first_cost = float(original["cost_usd"] or 0.0)
        total_cost = sum(float(row["cost_usd"] or 0.0) for row in attempts)
        overall["first_attempt_cost_usd"] += first_cost
        overall["total_cost_usd"] += total_cost
        _bump(buckets, keys, "first_attempt_cost_usd", first_cost)
        _bump(buckets, keys, "total_cost_usd", total_cost)

        unpriced = sum(1 for row in attempts if row["cost_usd"] is None)
        overall["unpriced_attempts"] += unpriced
        _bump(buckets, keys, "unpriced_attempts", unpriced)

        for attempt in attempts:
            if attempt["message_id"] is not None:
                origin_by_message[int(attempt["message_id"])] = keys
            signal = attempt["signal"]
            if signal in by_signal:
                by_signal[signal]["retries"] += 1
                by_signal[signal]["retry_cost_usd"] += float(attempt["cost_usd"] or 0.0)

    for row in database.assistant_message_mode_rows(owner, days):
        message_id = int(row["id"])
        if message_id in origin_by_message:
            continue  # a retried turn; retry_log already owns every attempt
        if row["mode_used"] == _NON_TURN_MODE:
            continue  # not a routed turn — see _NON_TURN_MODE
        keys = _turn_dimensions(row["mode_used"])
        origin_by_message[message_id] = keys
        cost = float(row["cost_usd"] or 0.0)
        overall["turns"] += 1
        overall["first_attempt_cost_usd"] += cost
        overall["total_cost_usd"] += cost
        _bump(buckets, keys, "turns")
        _bump(buckets, keys, "first_attempt_cost_usd", cost)
        _bump(buckets, keys, "total_cost_usd", cost)
        if row["cost_usd"] is None:
            overall["unpriced_attempts"] += 1
            _bump(buckets, keys, "unpriced_attempts", 1)

    for entry in database.correction_log_entries(owner, days):
        flagged = entry["message_id"]
        origin = origin_by_message.get(int(flagged)) if flagged is not None else None
        if origin is None:
            # The flagged answer is outside this window's turns (or predates
            # the ledger): fall back to the flag's OWN recorded decision,
            # which correction_log keeps precisely because the message it
            # names may be gone.
            origin = [
                entry["category"] or parse_mode_used(entry["mode_used"])[1],
                lane_from_mode_used(entry["mode_used"]),
            ]
        overall["corrections"] += 1
        _bump(buckets, origin, "corrections")

    return {
        "days": days,
        "overall": _finalize(overall),
        "by_category": {k: _finalize(v) for k, v in by_category.items()},
        "by_tier": {k: _finalize(v) for k, v in by_tier.items()},
        "by_signal": by_signal,
        "directional_width": _DIRECTIONAL_WIDTH,
    }
