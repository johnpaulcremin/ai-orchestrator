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
    """Route every prompt and record predicted vs expected tier."""
    results: list[dict[str, Any]] = []
    for item in dataset:
        decision = decide(item["prompt"])
        predicted = tier_from_mode_used(getattr(decision, "mode_used", ""))
        expected = item["expected_tier"]
        results.append(
            {
                "prompt": item["prompt"],
                "category": item.get("category", ""),
                "expected": expected,
                "predicted": predicted,
                "model": getattr(decision, "model", ""),
                "correct": predicted == expected,
            }
        )
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    # confusion[(expected, predicted)] = count
    confusion = Counter((r["expected"], r["predicted"]) for r in results)
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "confusion": {f"{e}->{p}": n for (e, p), n in sorted(confusion.items())},
    }
