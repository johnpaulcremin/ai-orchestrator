from __future__ import annotations

import argparse
import sys

from app.orchestrator import get_client
from app.routing import decide_route
from app.schemas import Mode

from .harness import evaluate, load_dataset, summarize


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
        help="Exit non-zero if accuracy is below this (0..1). Default 0 (never fails).",
    )
    args = parser.parse_args(argv)

    client = get_client()

    def decide(prompt: str):
        return decide_route(prompt, Mode.auto, client=client)

    dataset = load_dataset(args.dataset)
    results = evaluate(dataset, decide)
    summary = summarize(results)

    print(
        f"Tier accuracy:     {summary['correct']}/{summary['total']} "
        f"= {summary['accuracy']:.1%}"
    )
    print(
        f"Category accuracy: {summary['category_correct']}/{summary['total']} "
        f"= {summary['category_accuracy']:.1%}\n"
    )

    # Per-category table: how each task category is routed (tier) and classified.
    print(f"{'category':<18} {'n':>3}  {'tier':>7}  {'classified':>10}")
    print(f"{'-' * 18} {'-' * 3}  {'-' * 7}  {'-' * 10}")
    for cat, stats in summary["by_category"].items():
        print(
            f"{cat:<18} {stats['total']:>3}  "
            f"{stats['tier_accuracy']:>7.0%}  "
            f"{stats['category_accuracy']:>10.0%}"
        )

    print("\nConfusion (expected->predicted tier):")
    for key, count in summary["confusion"].items():
        print(f"  {key}: {count}")

    misroutes = [r for r in results if not r["correct"]]
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

    if summary["accuracy"] < args.min_accuracy:
        print(
            f"\nFAIL: accuracy {summary['accuracy']:.1%} < min {args.min_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
