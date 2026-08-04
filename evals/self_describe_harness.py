"""Pure scoring logic for the SELF_DESCRIBE trigger-accuracy eval — see
self_describe_run.py for the CLI that drives it against the real app (a
live model deciding whether to call the app_capabilities tool, or — for a
LiteLLM-routed model — the phrase-heuristic fallback).

Same `Prober` injection pattern as evals/injection_harness.py: a callable
that maps one dataset item to whether the self-description path actually
fired, so this is unit-tested offline in tests/test_evals.py with no
network and no real model call.

This measures the DECISION-GATE precision problem the tool description and
phrase-heuristic tightening (app/self_describe.py) were written to fix: a
misfire on an ordinary conversational follow-up ("why did that take two
attempts?") is exactly as much a bug here as a miss on a genuine
capabilities question ("what models do you use?") — unlike a soft-signal
feature, this is a binary decision with two distinct failure directions,
so `evaluate` tracks both a false-positive rate (misfired on a should_fire:
false item) and a false-negative rate (missed a should_fire: true item)
rather than one blended accuracy number.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "self_describe_dataset.json"

# A prober maps one dataset item (dict with question/should_fire/...) to
# whether the self-description path actually fired for it. In
# tests/test_evals.py this is a canned stand-in; in self_describe_run.py
# it's a real ask (with SELF_DESCRIBE=true) through the orchestrator,
# checking whether the appended "Verified capabilities" note is present.
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
                "question": item["question"],
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

    # false_negative_rate: of the genuine capabilities questions, how many
    # were MISSED -- an annoyance (the model answers without the verified
    # snapshot) but not a wrong-tool-firing incident.
    false_negative_rate = _rate(
        sum(1 for r in should_fire if not r["fired"]), len(should_fire)
    )
    # false_positive_rate: of the traps (meta-questions about a prior
    # answer, general AI questions, incidental "you"/"support" phrasing),
    # how many WRONGLY fired -- this is the "wrong tool firing again"
    # complaint the trigger-tightening was written to fix.
    false_positive_rate = _rate(
        sum(1 for r in should_not_fire if r["fired"]), len(should_not_fire)
    )

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "should_fire_total": len(should_fire),
        "false_negative_rate": false_negative_rate,
        "should_not_fire_total": len(should_not_fire),
        "false_positive_rate": false_positive_rate,
        "false_positive_ids": [r["id"] for r in should_not_fire if r["fired"]],
        "false_negative_ids": [r["id"] for r in should_fire if not r["fired"]],
    }
