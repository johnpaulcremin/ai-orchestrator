from __future__ import annotations

import argparse
import sys

from app.semantic_cache import _cosine_similarity, embed, threshold

from .semantic_cache_harness import evaluate, load_dataset, summarize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure semantic-cache precision (paraphrase hits vs "
        "near-miss false positives) against a labeled dataset, using the "
        "real embeddings API and this app's configured (or default) "
        "SEMANTIC_CACHE_THRESHOLD. Makes real API calls, so OPENAI_API_KEY "
        "must be set."
    )
    parser.add_argument("--dataset", help="Path to a dataset JSON file.", default=None)
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="Exit non-zero if overall accuracy is below this (0..1). Default 0 (never fails).",
    )
    parser.add_argument(
        "--max-false-positive-rate",
        type=float,
        default=0.0,
        help=(
            "Exit non-zero if the false-positive rate (near-misses that "
            "wrongly cleared the threshold) exceeds this. Default 0 -- ANY "
            "false positive fails by default, since this is the direction "
            "that can serve a confidently wrong cached answer."
        ),
    )
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    results = evaluate(dataset, embed, _cosine_similarity, threshold())
    summary = summarize(results)

    print(f"Threshold: {threshold():.4f}\n")
    print(
        f"Overall accuracy:    {summary['correct']}/{summary['total']} = {summary['accuracy']:.1%}"
    )
    print(
        f"Paraphrase hit rate: "
        f"{round(summary['hit_rate'] * summary['should_match_total'])}/{summary['should_match_total']} "
        f"= {summary['hit_rate']:.1%}"
    )
    print(
        f"False positive rate: "
        f"{round(summary['false_positive_rate'] * summary['should_not_match_total'])}/"
        f"{summary['should_not_match_total']} = {summary['false_positive_rate']:.1%}"
    )

    # Full score distribution, BOTH directions, sorted -- not just the
    # wrong ones. A pair scoring 0.80 against a 0.96 threshold is a MISS,
    # not a failure worth a line in the "missed" list below, but it's
    # exactly the number anyone discussing whether to move the threshold
    # needs to see next to the trap pairs' own scores -- where the two
    # distributions actually overlap (or don't) is the real picture, and
    # the old output only ever showed the wrong-direction subset.
    should_match = sorted(
        (r for r in results if r["expected_match"]),
        key=lambda r: r["similarity"],
        reverse=True,
    )
    should_not_match = sorted(
        (r for r in results if not r["expected_match"]),
        key=lambda r: r["similarity"],
        reverse=True,
    )
    print(f"\nShould-match scores (threshold {threshold():.4f}), highest first:")
    for r in should_match:
        mark = "HIT " if r["predicted_match"] else "miss"
        print(f"  [{mark}] {r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}")
    print("\nTrap (must-not-match) scores, highest first:")
    for r in should_not_match:
        mark = "FALSE POSITIVE" if r["predicted_match"] else "correctly clear"
        print(
            f"  [{mark:>14}] {r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}"
        )

    misses = [r for r in results if not r["correct"] and r["expected_match"]]
    if misses:
        print("\nMissed paraphrases (should have matched, didn't):")
        for r in misses:
            print(
                f"  similarity={r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}"
            )

    false_positives = [
        r for r in results if not r["correct"] and not r["expected_match"]
    ]
    if false_positives:
        print("\nFalse positives (should NOT have matched, did):")
        for r in false_positives:
            print(
                f"  similarity={r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}"
            )

    failed = False
    if summary["accuracy"] < args.min_accuracy:
        print(
            f"\nFAIL: accuracy {summary['accuracy']:.1%} < min {args.min_accuracy:.1%}",
            file=sys.stderr,
        )
        failed = True
    if summary["false_positive_rate"] > args.max_false_positive_rate:
        print(
            f"\nFAIL: false-positive rate {summary['false_positive_rate']:.1%} "
            f"> max {args.max_false_positive_rate:.1%}",
            file=sys.stderr,
        )
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
