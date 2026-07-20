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


def evaluate(dataset: list[dict[str, Any]], decide: Decider) -> list[dict[str, Any]]:
    """Route every prompt and record predicted vs expected tier AND category."""
    results: list[dict[str, Any]] = []
    for item in dataset:
        decision = decide(item["prompt"])
        predicted = tier_from_mode_used(getattr(decision, "mode_used", ""))
        expected = item["expected_tier"]
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
        by_category[cat] = {
            "total": n,
            "tier_correct": tier_hits,
            "tier_accuracy": _rate(tier_hits, n),
            "category_correct": cat_hits,
            "category_accuracy": _rate(cat_hits, n),
        }

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "category_correct": category_correct,
        "category_accuracy": _rate(category_correct, total),
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items())},
        "by_category": by_category,
    }
