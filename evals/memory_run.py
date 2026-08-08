"""CLI for the cross-conversation-memory precision eval — see
memory_harness.py for the pure scoring logic this drives.

THE FALSE-POSITIVE GATE IS NOT ZERO, and that is deliberate. It was 0 (with a
`--min-accuracy 0.9` alongside it in the runbook), and NEITHER was reachable:
two of the seven must-not-recall traps — "what I said about it" vs "about
that" (0.95674) and "March 5th release" vs "March 12th release" (0.89526) —
score ABOVE every genuine recall pair but one, so the only threshold with zero
false positives is one that recalls nothing at all (accuracy 46.7%, recall
0/8). The accuracy gate was likewise capped at 73.3% by the same overlap. A
gate no configuration can satisfy is not a gate; it is a permanently red light
nobody looks at, and this one was red while a REAL regression (four traps
firing at the old 0.75 threshold, not two) sat underneath it unnoticed.

The default below is set just above what the shipped MEMORY_THRESHOLD actually
achieves, so `python -m evals.memory_run` with no flags passes on a healthy
system and fails the moment a third trap starts clearing. See
app/memory.py's _DEFAULT_THRESHOLD for the threshold side of the same
measurement, and evals/separability.py for the ceiling this run prints.
"""

from __future__ import annotations

import argparse
import sys

from app.memory import threshold
from app.semantic_cache import _cosine_similarity, embed

from .memory_harness import evaluate, load_dataset, summarize
from .separability import format_ceiling

# 2 of the 7 traps clear the shipped threshold (2/7 = 28.6%) and cannot be
# removed by any threshold — see the module docstring. 0.29 is the smallest
# round number above that: the current, healthy state passes, and a THIRD
# false positive (3/7 = 42.9%) fails. Deliberately not padded further —
# headroom here is exactly the room a regression hides in.
_DEFAULT_MAX_FALSE_POSITIVE_RATE = 0.29


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure cross-conversation-memory recall precision "
        "(relevant-past-exchange hits vs unrelated/referentially-ambiguous "
        "false positives) against a labeled dataset, using the real "
        "embeddings API and this app's configured (or default) "
        "MEMORY_THRESHOLD. Makes real API calls, so OPENAI_API_KEY must be "
        "set."
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
        default=_DEFAULT_MAX_FALSE_POSITIVE_RATE,
        help=(
            "Exit non-zero if the false-positive rate (unrelated pairs that "
            f"wrongly cleared the threshold) exceeds this. Default "
            f"{_DEFAULT_MAX_FALSE_POSITIVE_RATE} -- NOT zero, because zero is "
            "unreachable on this dataset at any threshold that recalls "
            "anything at all; see the module docstring."
        ),
    )
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    results = evaluate(dataset, embed, _cosine_similarity, threshold())
    summary = summarize(results)

    print(f"Threshold: {threshold():.4f}\n")
    print(
        f"Overall accuracy: {summary['correct']}/{summary['total']} = {summary['accuracy']:.1%}"
    )
    # See semantic_cache_run.py's identical placement, and
    # evals/separability.py for why a raw figure here misleads.
    print(format_ceiling(summary, "recall pair"))
    print(
        f"Recall rate:      "
        f"{round(summary['recall_rate'] * summary['should_match_total'])}/{summary['should_match_total']} "
        f"= {summary['recall_rate']:.1%}"
    )
    print(
        f"False positive rate: "
        f"{round(summary['false_positive_rate'] * summary['should_not_match_total'])}/"
        f"{summary['should_not_match_total']} = {summary['false_positive_rate']:.1%}"
    )

    # Full score distribution, BOTH directions, sorted -- not just the
    # wrong ones. See semantic_cache_run.py's identical section for why:
    # a relevant pair scoring low and a trap pair scoring high are only
    # meaningfully compared side by side, sorted, not scattered across
    # separate "missed"/"false positive" lists that only show the wrong
    # direction each.
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
    print(f"\nShould-recall scores (threshold {threshold():.4f}), highest first:")
    for r in should_match:
        mark = "HIT " if r["predicted_match"] else "miss"
        print(f"  [{mark}] {r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}")
    print("\nTrap (must-not-recall) scores, highest first:")
    for r in should_not_match:
        mark = "FALSE POSITIVE" if r["predicted_match"] else "correctly clear"
        print(
            f"  [{mark:>14}] {r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}"
        )

    misses = [r for r in results if not r["correct"] and r["expected_match"]]
    if misses:
        print("\nMissed relevant pairs (should have recalled, didn't):")
        for r in misses:
            print(
                f"  similarity={r['similarity']:.4f} :: {r['stored']!r} <-> {r['query']!r}"
            )

    false_positives = [
        r for r in results if not r["correct"] and not r["expected_match"]
    ]
    if false_positives:
        print("\nFalse positives (should NOT have recalled, did):")
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
