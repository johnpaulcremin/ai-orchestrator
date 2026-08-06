"""Pure scoring logic for the AUTO_WORKFLOW multi-artefact eval — see
multipart_run.py for the CLI that drives it against the real classifier.

Same `Prober` injection pattern as evals/self_describe_harness.py: a callable
mapping one dataset item to whether the classifier judged it multi-part, so
this is unit-tested offline in tests/test_evals.py with no network and no
real model call.

This measures a DECISION GATE with two very unequal failure directions, so
it reports them separately rather than as one blended accuracy number:

* A false positive routes an ordinary question into a multi-step workflow.
  That question then costs several model calls instead of one and takes
  correspondingly longer, for no benefit — the single-shot path would have
  answered it perfectly well. This is the expensive direction.
* A false negative just leaves a genuinely multi-artefact request on the
  single-shot path, which is exactly where it went before AUTO_WORKFLOW
  existed. Nothing regresses; the feature simply didn't help that time.

The default gates in multipart_run.py are set accordingly — tight on false
positives, loose on false negatives — mirroring self_describe_run.py, which
faces the same shape of problem from the other end.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "multipart_dataset.json"

# Maps one dataset item to whether the router judged it multi-part. Canned in
# tests/test_evals.py; a real classifier call in multipart_run.py.
Prober = Callable[[dict[str, Any]], bool]


def load_dataset(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else DATASET_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def evaluate(dataset: list[dict[str, Any]], probe: Prober) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in dataset:
        fired = bool(probe(item))
        expected = bool(item["should_fire"])
        results.append(
            {
                "id": item["id"],
                "prompt": item["prompt"],
                "should_fire": expected,
                "fired": fired,
                "correct": fired == expected,
            }
        )
    return results


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    correct = sum(1 for r in results if r["correct"])

    should_fire = [r for r in results if r["should_fire"]]
    should_not_fire = [r for r in results if not r["should_fire"]]

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "should_fire_total": len(should_fire),
        "false_negative_rate": _rate(
            sum(1 for r in should_fire if not r["fired"]), len(should_fire)
        ),
        "should_not_fire_total": len(should_not_fire),
        "false_positive_rate": _rate(
            sum(1 for r in should_not_fire if r["fired"]), len(should_not_fire)
        ),
        "false_positive_ids": [r["id"] for r in should_not_fire if r["fired"]],
        "false_negative_ids": [r["id"] for r in should_fire if not r["fired"]],
    }
