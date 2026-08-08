"""The ceiling a threshold-scored eval cannot exceed, whatever the threshold.

evals/run.py reports a ceiling too, but a different one: there, items are
UNSCOREABLE BY CONSTRUCTION (routed into a lane the dataset has no label for)
and get excluded from the denominator. Here every item is scored — the limit
is SEPARABILITY. Both the semantic-cache and memory datasets deliberately
include near-miss traps (entity swaps, date swaps, unit swaps) engineered to
sit close to genuine matches, and embedding similarity cannot pull those
apart: the score distributions physically overlap, so some pair is misjudged
at EVERY threshold.

That makes a raw accuracy figure read like a failing grade when it may be the
best the fixture set admits. The memory eval sat at 60.0% against a
`--min-accuracy 0.9` gate that no threshold could ever have satisfied — the
same misreading evals/run.py's config ceiling was added to prevent, arriving
by a different route.

`ceiling` below sweeps every threshold the observed scores can distinguish and
reports the best accuracy reachable, the threshold that reaches it, whether
the distributions overlap at all, and the best accuracy reachable while
holding false positives at zero (a stricter, separately interesting bound: for
the semantic cache a false positive serves a confidently wrong answer, so the
zero-FP figure is the one an operator actually has to live within).

Shared by both harnesses rather than written twice: they score identical
result shapes, and a ceiling that drifted between them would be worse than no
ceiling at all.
"""

from __future__ import annotations

from typing import Any


def _accuracy_at(results: list[dict[str, Any]], threshold: float) -> int:
    return sum(
        1 for r in results if (r["similarity"] >= threshold) == r["expected_match"]
    )


def _false_positives_at(results: list[dict[str, Any]], threshold: float) -> int:
    return sum(
        1 for r in results if not r["expected_match"] and r["similarity"] >= threshold
    )


def ceiling(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-achievable accuracy over every threshold, plus the overlap that
    caps it. Keys mirror the `*_ceiling` naming evals/harness.py uses.

    The candidate thresholds are each observed score and each score plus an
    epsilon — between two adjacent observed scores nothing can change, so this
    sweep is exhaustive, not a sample.
    """
    total = len(results)
    if not total:
        return {
            "best_accuracy": 0.0,
            "best_correct": 0,
            "best_threshold": None,
            "zero_fp_best_accuracy": 0.0,
            "zero_fp_best_threshold": None,
            "overlaps": False,
            "highest_trap": None,
            "lowest_true_match": None,
        }

    candidates = sorted(
        {r["similarity"] for r in results} | {r["similarity"] + 1e-9 for r in results}
    )
    scored = [
        (_accuracy_at(results, t), _false_positives_at(results, t), t)
        for t in candidates
    ]
    best_correct, _fp, best_threshold = max(scored, key=lambda row: (row[0], -row[2]))
    zero_fp = [row for row in scored if row[1] == 0]
    zero_fp_correct, _, zero_fp_threshold = (
        max(zero_fp, key=lambda row: (row[0], -row[2])) if zero_fp else (0, 0, None)
    )

    traps = [r["similarity"] for r in results if not r["expected_match"]]
    true_matches = [r["similarity"] for r in results if r["expected_match"]]
    highest_trap = max(traps) if traps else None
    lowest_true_match = min(true_matches) if true_matches else None

    return {
        "best_accuracy": best_correct / total,
        "best_correct": best_correct,
        "best_threshold": best_threshold,
        "zero_fp_best_accuracy": zero_fp_correct / total,
        "zero_fp_best_threshold": zero_fp_threshold,
        # True when at least one must-not-match pair scores at or above at
        # least one genuine match — i.e. no threshold separates them, and the
        # ceiling below 100% is a property of the fixtures, not the code.
        "overlaps": (
            highest_trap is not None
            and lowest_true_match is not None
            and highest_trap >= lowest_true_match
        ),
        "highest_trap": highest_trap,
        "lowest_true_match": lowest_true_match,
    }


def format_ceiling(summary: dict[str, Any], label: str) -> str:
    """The ceiling block both run.py CLIs print under their headline, in the
    same shape evals/run.py prints its configuration ceiling."""
    ceiling_info = summary["ceiling"]
    total = summary["total"]
    lines = [
        f"{'':<18} ceiling {ceiling_info['best_correct']}/{total} = "
        f"{ceiling_info['best_accuracy']:.1%} -- the best ANY threshold reaches "
        f"on this dataset"
    ]
    if ceiling_info["overlaps"]:
        lines.append(
            f"{'':<18} why: score distributions overlap -- the highest "
            f"must-not-match trap ({ceiling_info['highest_trap']:.4f}) scores "
            f"at or above\n"
            f"{'':<18} the lowest genuine {label} "
            f"({ceiling_info['lowest_true_match']:.4f}), so some pair is "
            "misjudged at every threshold."
        )
    else:
        lines.append(
            f"{'':<18} the distributions do not overlap -- 100% is reachable "
            "with the right threshold."
        )
    if ceiling_info["zero_fp_best_threshold"] is not None:
        lines.append(
            f"{'':<18} holding false positives at ZERO caps accuracy at "
            f"{ceiling_info['zero_fp_best_accuracy']:.1%} "
            f"(threshold ~{ceiling_info['zero_fp_best_threshold']:.4f})."
        )
    else:
        lines.append(
            f"{'':<18} no threshold reaches zero false positives on this dataset."
        )
    return "\n".join(lines)
