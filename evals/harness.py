from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "dataset.json"

# A decider maps a prompt to something with a `.mode_used` attribute (a
# RouteDecision, or any stand-in in tests).
Decider = Callable[[str], Any]


def load_dataset(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else DATASET_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def tier_from_mode_used(mode_used: str) -> str | None:
    """Map a mode_used string (e.g. 'auto->smart') to its tier, or None."""
    value = (mode_used or "").lower()
    if "smart" in value:
        return "smart"
    if "fast" in value:
        return "fast"
    return None


# Bucket for a predicted (or, defensively, expected) tier that couldn't be
# resolved to "fast"/"smart" -- e.g. a live run where free-lane routing
# (see app/free_tier.py) resolved a prompt to `mode_used="auto->free:
# <model>"`, which tier_from_mode_used can't map to either tier. A real
# `None` here isn't wrong (a genuinely unparseable/unroutable result is a
# real outcome worth surfacing, not an error to hide), but BUG HISTORY: it
# used to flow straight into `results["predicted"]`, and summarize()'s
# `sorted(confusion.items())` crashed with a TypeError the first time a
# live run actually produced one, since Python can't order `None` against
# a string. Bucketed as this string instead, so confusion/by_category stay
# sortable and every unparsed case is still visible (see UNPARSED_TIER
# used below and by evals/run.py's own reporting).
UNPARSED_TIER = "unparsed"


# Routing lanes this dataset structurally CANNOT score. Both are opt-in
# configuration, not routing mistakes: `expected_tier` is a fast/smart binary,
# so an item sent to a third lane has no label to be graded against — the
# dataset cannot say whether that lane was the right answer.
#
# This is why a raw tier percentage is misleading rather than merely low. With
# OPENAI_MODEL_BUDGET pointed at a local model, every low-complexity
# fast-category prompt routes to `auto->budget` and lands here; on the
# 55-prompt default dataset that is 20 items, so the raw score can never
# exceed 35/55 = 63.6% no matter how perfectly the router behaves. Read cold,
# that looks like a serious regression. summarize() below therefore reports
# the ceiling and the score as a fraction of it, and evals/run.py prints the
# configuration that produced it.
#
# Keyed by the substring that identifies the lane in `mode_used`, valued by
# the setting that turns it on — so the report can name the cause, not just
# the symptom.
UNSCOREABLE_LANES: dict[str, str] = {
    "budget": "OPENAI_MODEL_BUDGET",
    "free": "FREE_TIER_ROUTING",
}


def unscoreable_lane(mode_used: str) -> str | None:
    """Which UNSCOREABLE_LANES lane this `mode_used` landed in, or None.

    Only consulted for a result the fast/smart parser already failed on, so a
    genuinely unparseable output with no known cause (e.g. `auto->clarify`,
    or something new) still counts against the score rather than being
    quietly excused — the exclusion has to be earned by a named lane."""
    value = (mode_used or "").lower()
    for lane in UNSCOREABLE_LANES:
        if lane in value:
            return lane
    return None


def evaluate(dataset: list[dict[str, Any]], decide: Decider) -> list[dict[str, Any]]:
    """Route every prompt and record predicted vs expected tier AND category."""
    results: list[dict[str, Any]] = []
    for item in dataset:
        decision = decide(item["prompt"])
        mode_used = getattr(decision, "mode_used", "")
        predicted = tier_from_mode_used(mode_used) or UNPARSED_TIER
        # Defensive: a dataset entry with a missing/malformed expected_tier
        # would hit the exact same sort crash from the other side.
        expected = item["expected_tier"] or UNPARSED_TIER
        expected_category = item.get("category", "")
        predicted_category = getattr(decision, "category", "") or ""
        results.append(
            {
                "prompt": item["prompt"],
                "category": expected_category,
                "expected": expected,
                "predicted": predicted,
                "predicted_category": predicted_category,
                "model": getattr(decision, "model", ""),
                "mode_used": mode_used,
                # Which config-enabled lane (if any) put this item beyond what
                # a fast/smart dataset can grade — see unscoreable_lane. Only
                # ever set for a result the tier parser failed on.
                "unscoreable_lane": (
                    unscoreable_lane(mode_used) if predicted == UNPARSED_TIER else None
                ),
                "correct": predicted == expected,
                # Category is "correct" only when the classifier named the same
                # category (an empty prediction — heuristic fallback — never counts).
                "category_correct": bool(predicted_category)
                and predicted_category == expected_category,
            }
        )
    return results


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    category_correct = sum(1 for r in results if r["category_correct"])
    # confusion[(expected, predicted)] = count
    confusion = Counter((r["expected"], r["predicted"]) for r in results)

    # Per-category breakdown: tier accuracy AND category-classification accuracy.
    by_category: dict[str, Any] = {}
    for cat in sorted({r["category"] for r in results if r["category"]}):
        rows = [r for r in results if r["category"] == cat]
        n = len(rows)
        tier_hits = sum(1 for r in rows if r["correct"])
        cat_hits = sum(1 for r in rows if r["category_correct"])
        # Same ceiling treatment as the headline numbers below, and needed
        # most here: the whole POINT of the budget tier is that it takes
        # low-complexity fast-category work, so exactly the fast categories
        # (quick_fact, casual_chat, summarization, simple_transform) show 0%
        # tier accuracy when it's on. Per-category is where someone looks
        # after seeing a low headline, so it must not repeat the same trap.
        tier_unscoreable = sum(1 for r in rows if r["unscoreable_lane"])
        tier_achievable = n - tier_unscoreable
        by_category[cat] = {
            "total": n,
            "tier_correct": tier_hits,
            "tier_accuracy": _rate(tier_hits, n),
            "tier_unscoreable": tier_unscoreable,
            "tier_achievable": tier_achievable,
            "tier_achievable_accuracy": _rate(tier_hits, tier_achievable),
            "category_correct": cat_hits,
            "category_accuracy": _rate(cat_hits, n),
        }

    # --- ceilings ------------------------------------------------------------
    # Both headline metrics have a maximum that MOVES WITH CONFIGURATION, so
    # each is reported alongside what it could possibly have scored. Raw
    # accuracy stays the primary number (it is what actually happened); the
    # achievable fraction is what tells you whether the router did badly or
    # was simply asked a question this dataset cannot grade.
    #
    # TIER: an item routed into a lane the fast/smart dataset has no label for
    # (see UNSCOREABLE_LANES).
    tier_unscoreable_rows = [r for r in results if r["unscoreable_lane"]]
    by_lane = Counter(str(r["unscoreable_lane"]) for r in tier_unscoreable_rows)
    # Broken out because excluding these from the denominator could otherwise
    # HIDE a real misroute: a smart-expected prompt landing in a cheaper lane
    # is a genuine problem, not a scoring artefact.
    unscoreable_expected = Counter(str(r["expected"]) for r in tier_unscoreable_rows)
    tier_achievable = total - len(tier_unscoreable_rows)

    # CATEGORY: an item the classifier never saw has no predicted category to
    # grade. Two causes, both configuration-driven: ROUTER_PREFILTER's
    # shortcut deliberately skips the classifier for an obvious prompt, and
    # the keyword heuristic takes over when the classifier is unavailable.
    # (evals/run.py prints the prefilter's state so the two can be told apart
    # — an unclassified count that ISN'T explained by the prefilter means the
    # classifier was failing, which is a real finding, not a ceiling.)
    category_unscoreable = sum(1 for r in results if not r["predicted_category"])
    category_achievable = total - category_unscoreable

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "tier_unscoreable": len(tier_unscoreable_rows),
        "tier_unscoreable_by_lane": dict(sorted(by_lane.items())),
        "tier_unscoreable_by_expected_tier": dict(sorted(unscoreable_expected.items())),
        "tier_achievable": tier_achievable,
        "tier_ceiling": _rate(tier_achievable, total),
        "tier_achievable_accuracy": _rate(correct, tier_achievable),
        "category_correct": category_correct,
        "category_accuracy": _rate(category_correct, total),
        "category_unscoreable": category_unscoreable,
        "category_achievable": category_achievable,
        "category_ceiling": _rate(category_achievable, total),
        "category_achievable_accuracy": _rate(category_correct, category_achievable),
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items())},
        "by_category": by_category,
    }
