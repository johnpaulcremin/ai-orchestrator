from __future__ import annotations

import argparse
import sys
from typing import Any

from app.orchestrator import get_client

# _prefilter_enabled is private, but importing it beats restating its
# env-parsing here and letting the two drift — this script already reaches
# into decide_route/get_client the same way.
from app.routing import _prefilter_enabled, decide_route
from app.schemas import Mode
from app.settings import get_model_overrides, model_setting

from .harness import UNSCOREABLE_LANES, UNPARSED_TIER, evaluate, load_dataset, summarize


def _print_ceiling_configuration() -> bool:
    """The configuration that determines what this run could possibly score.

    Returns whether the budget tier is enabled, so the scoring below is driven
    by the SAME resolution that was just printed. Resolving it twice would let
    the report explain a ceiling it hadn't actually applied.

    Printed FIRST, and unconditionally, because the failure mode it exists to
    prevent is reading a raw percentage cold: with a budget tier configured,
    20 of the 55 default prompts route to `auto->budget`, which a fast/smart
    dataset has no label for, so 63.6% is a perfect score rather than a
    regression. That cost a day of suspicion once.
    """
    overrides = get_model_overrides()
    budget_model = model_setting("OPENAI_MODEL_BUDGET", "", overrides)

    print("Configuration affecting the achievable score:")
    if budget_model:
        print(
            f"  budget tier:      ENABLED (OPENAI_MODEL_BUDGET={budget_model})\n"
            "                    -> a low-complexity fast-category prompt routes to "
            "auto->budget,\n"
            "                       graded against its expected_tier_with_budget "
            "label where it has\n"
            "                       one, and excluded from the denominator where "
            "it does not."
        )
    else:
        print(
            "  budget tier:      off (OPENAI_MODEL_BUDGET unset)\n"
            "                    -> every prompt is gradeable as fast/smart."
        )
    if _prefilter_enabled():
        print(
            "  router prefilter: ENABLED (ROUTER_PREFILTER)\n"
            "                    -> an obvious prompt skips the classifier, so it "
            "has no\n"
            "                       predicted category to grade."
        )
    else:
        print(
            "  router prefilter: off (ROUTER_PREFILTER=false)\n"
            "                    -> every prompt reaches the classifier."
        )
    print()
    return bool(budget_model)


def _print_metric(
    label: str,
    correct: int,
    total: int,
    raw: float,
    achievable: int,
    ceiling: float,
    achievable_accuracy: float,
    unscoreable: int,
    reason: str,
) -> None:
    """One headline metric with its ceiling. Raw first (it is what actually
    happened), then the same score as a fraction of what was achievable."""
    print(
        f"{label:<18} {correct}/{total} = {raw:.1%} raw"
        f"   |   {correct}/{achievable} = {achievable_accuracy:.1%} of achievable"
    )
    if unscoreable:
        print(
            f"{'':<18} ceiling {achievable}/{total} = {ceiling:.1%} -- "
            f"{unscoreable} item(s) unscoreable by construction ({reason})"
        )
    else:
        print(
            f"{'':<18} ceiling {achievable}/{total} = {ceiling:.1%} -- nothing excluded"
        )


def _lane_reason(summary: dict[str, Any]) -> str:
    parts = [
        f"{n} routed to auto->{lane} via {UNSCOREABLE_LANES[lane]}"
        for lane, n in summary["tier_unscoreable_by_lane"].items()
    ]
    return "; ".join(parts) or "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure auto-routing classifier accuracy against a labeled dataset. "
        "Makes real router (OPENAI_MODEL_ROUTER) calls, so OPENAI_API_KEY must be set."
    )
    parser.add_argument("--dataset", help="Path to a dataset JSON file.", default=None)
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="Exit non-zero if RAW tier accuracy is below this (0..1). Default 0 "
        "(never fails). Note this is capped by configuration -- with a budget "
        "tier configured it cannot reach 100%%, so a high value here is "
        "unreachable by construction; see --min-achievable-accuracy.",
    )
    parser.add_argument(
        "--min-achievable-accuracy",
        type=float,
        default=0.0,
        help="Exit non-zero if tier accuracy AS A FRACTION OF ACHIEVABLE is below "
        "this (0..1). Default 0 (never fails). This is the config-independent "
        "gate: items routed into a lane this dataset has no label for are "
        "excluded from the denominator, so the same threshold means the same "
        "thing whether or not a budget/free lane is enabled.",
    )
    args = parser.parse_args(argv)

    client = get_client()

    def decide(prompt: str):
        return decide_route(prompt, Mode.auto, client=client)

    dataset = load_dataset(args.dataset)
    # Printed before scoring, and its return value drives the scoring: the
    # budget lane's expectations depend on whether that lane exists (see
    # harness.expected_tier), and the report must describe the run it graded.
    budget_tier_enabled = _print_ceiling_configuration()
    results = evaluate(dataset, decide, budget_tier_enabled=budget_tier_enabled)
    summary = summarize(results)

    _print_metric(
        "Tier accuracy:",
        summary["correct"],
        summary["total"],
        summary["accuracy"],
        summary["tier_achievable"],
        summary["tier_ceiling"],
        summary["tier_achievable_accuracy"],
        summary["tier_unscoreable"],
        _lane_reason(summary),
    )
    # An exclusion must never hide a real misroute: a SMART-expected prompt in
    # a cheaper lane is a genuine problem, and dropping it from the
    # denominator would launder it into a better-looking score.
    smart_excluded = summary["tier_unscoreable_by_expected_tier"].get("smart", 0)
    if smart_excluded:
        print(
            f"{'':<18} WARNING: {smart_excluded} of those expected SMART -- a "
            "smart prompt in a cheaper\n"
            f"{'':<18} lane is a real misroute, not a scoring artefact. "
            "Inspect it below."
        )
    _print_metric(
        "Category accuracy:",
        summary["category_correct"],
        summary["total"],
        summary["category_accuracy"],
        summary["category_achievable"],
        summary["category_ceiling"],
        summary["category_achievable_accuracy"],
        summary["category_unscoreable"],
        "classifier never ran (ROUTER_PREFILTER shortcut, or heuristic fallback)",
    )
    print()

    # Per-category table: how each task category is routed (tier) and
    # classified. `tier` is raw; `tier/ach` is the same number over only the
    # items this dataset could grade, and `ungr.` is how many it could not —
    # without those two columns the fast categories read as 0% whenever the
    # budget tier is on, which is the headline trap repeated per row.
    print(
        f"{'category':<18} {'n':>3}  {'tier':>7}  {'tier/ach':>8}  "
        f"{'ungr.':>5}  {'classified':>10}"
    )
    print(f"{'-' * 18} {'-' * 3}  {'-' * 7}  {'-' * 8}  {'-' * 5}  {'-' * 10}")
    for cat, stats in summary["by_category"].items():
        achievable = (
            f"{stats['tier_achievable_accuracy']:.0%}"
            if stats["tier_achievable"]
            else "n/a"
        )
        print(
            f"{cat:<18} {stats['total']:>3}  "
            f"{stats['tier_accuracy']:>7.0%}  "
            f"{achievable:>8}  "
            f"{stats['tier_unscoreable']:>5}  "
            f"{stats['category_accuracy']:>10.0%}"
        )

    print("\nConfusion (expected->predicted tier):")
    for key, count in summary["confusion"].items():
        print(f"  {key}: {count}")

    # Genuine misroutes only: an item excluded from the ceiling is listed in
    # its own section below instead. Listing it in both was the old report's
    # sharpest edge — 20 "misroutes" that were nothing of the kind.
    misroutes = [r for r in results if not r["correct"] and not r["unscoreable_lane"]]
    if misroutes:
        print("\nTier misroutes:")
        for r in misroutes:
            print(
                f"  [{r['category']}] expected {r['expected']}, got {r['predicted']} "
                f"({r['model']}) :: {r['prompt'][:70]}"
            )

    misclassified = [
        r for r in results if not r["category_correct"] and r["predicted_category"]
    ]
    if misclassified:
        print("\nCategory misclassifications (tier may still be correct):")
        for r in misclassified:
            print(
                f"  expected {r['category']}, got {r['predicted_category']} "
                f":: {r['prompt'][:60]}"
            )

    # A live run can return mode_used that tier_from_mode_used can't map to
    # either tier -- split by whether a KNOWN, config-enabled lane explains it
    # (excluded from the ceiling above) or nothing does (still counted
    # against the score, and the more interesting of the two).
    unparsed = [r for r in results if r["predicted"] == UNPARSED_TIER]
    explained = [r for r in unparsed if r["unscoreable_lane"]]
    unexplained = [r for r in unparsed if not r["unscoreable_lane"]]
    if explained:
        print(
            f"\nUnscoreable by construction: {len(explained)}/{summary['total']} "
            "(routed to a lane this dataset has no fast/smart label for --\n"
            "excluded from the achievable denominator above, NOT counted as "
            "misroutes):"
        )
        for r in explained:
            print(
                f"  [{r['expected']}-expected] mode_used={r['mode_used']!r} "
                f"model={r['model']!r} :: {r['prompt'][:60]}"
            )
    if unexplained:
        print(
            f"\nUnparsed router output with NO known cause: "
            f"{len(unexplained)}/{summary['total']} (counted against the score):"
        )
        for r in unexplained:
            print(
                f"  mode_used={r['mode_used']!r} model={r['model']!r} "
                f":: {r['prompt'][:60]}"
            )

    # The report above goes to stdout, the verdict below to stderr. Flush
    # first: piped through a shell the two buffer independently, and a FAIL
    # line appearing ABOVE the numbers that explain it is precisely the
    # misreading this whole section exists to prevent.
    sys.stdout.flush()

    if summary["accuracy"] < args.min_accuracy:
        print(
            f"\nFAIL: raw accuracy {summary['accuracy']:.1%} < "
            f"--min-accuracy {args.min_accuracy:.1%}"
            + (
                f"\n      NOTE: the ceiling this run could reach is "
                f"{summary['tier_ceiling']:.1%} "
                f"({summary['tier_unscoreable']} item(s) unscoreable by "
                f"construction -- {_lane_reason(summary)}),\n"
                f"      so --min-accuracy {args.min_accuracy:.1%} is "
                "unreachable under this configuration. Score as a fraction of "
                f"achievable was {summary['tier_achievable_accuracy']:.1%}; "
                "gate on that with --min-achievable-accuracy instead."
                if args.min_accuracy > summary["tier_ceiling"]
                else ""
            ),
            file=sys.stderr,
        )
        return 1
    if summary["tier_achievable_accuracy"] < args.min_achievable_accuracy:
        print(
            f"\nFAIL: achievable accuracy "
            f"{summary['tier_achievable_accuracy']:.1%} < "
            f"--min-achievable-accuracy {args.min_achievable_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
