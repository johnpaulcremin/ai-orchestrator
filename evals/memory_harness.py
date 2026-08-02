"""Pure scoring logic for the cross-conversation-memory precision eval — see
evals/memory_run.py for the CLI that drives it with real embeddings.

Same shape as evals/semantic_cache_harness.py (a labeled `(stored, query,
should_match)` pair set scored against a threshold), but a distinct
harness/dataset rather than a shared one: memory's failure mode is softer
(a false positive here just injects a possibly-irrelevant past exchange the
model is told to use its own judgment on — see app/memory.py's module
docstring — not a served wrong answer), and memory has no context-free
structural guardrail the way semantic-cache does, so its false-positive
rate is worth tracking independently at its own (looser, 0.75 default)
threshold rather than folded into the semantic-cache numbers.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "memory_dataset.json"

# Maps a piece of text to its embedding vector (or None on failure).
Embedder = Callable[[str], list[float] | None]
Similarity = Callable[[list[float], list[float]], float]


def load_dataset(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else DATASET_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def evaluate(
    dataset: list[dict[str, Any]],
    embed: Embedder,
    cosine_similarity: Similarity,
    threshold: float,
) -> list[dict[str, Any]]:
    """Embed both sides of every pair and record whether the pair would have
    been recalled at `threshold` (see memory.threshold()) against whether it
    SHOULD have (should_match in the dataset)."""
    results: list[dict[str, Any]] = []
    for item in dataset:
        stored_vector = embed(item["stored"])
        query_vector = embed(item["query"])
        similarity = (
            cosine_similarity(stored_vector, query_vector)
            if stored_vector is not None and query_vector is not None
            else 0.0
        )
        predicted_match = similarity >= threshold
        expected_match = bool(item["should_match"])
        results.append(
            {
                "stored": item["stored"],
                "query": item["query"],
                "expected_match": expected_match,
                "predicted_match": predicted_match,
                "similarity": similarity,
                "correct": predicted_match == expected_match,
            }
        )
    return results


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    correct = sum(1 for r in results if r["correct"])

    should_match = [r for r in results if r["expected_match"]]
    should_not_match = [r for r in results if not r["expected_match"]]

    # recall_rate: of the genuinely relevant pairs, how many actually
    # cleared the threshold -- a miss here just means a helpful past
    # exchange doesn't get surfaced (ok, not dangerous).
    recall_rate = _rate(
        sum(1 for r in should_match if r["predicted_match"]), len(should_match)
    )
    # false_positive_rate: of the unrelated/referentially-ambiguous pairs,
    # how many WRONGLY cleared the threshold -- injects an irrelevant (or
    # worse, misleadingly relevant-looking) snippet into a new turn.
    false_positive_rate = _rate(
        sum(1 for r in should_not_match if r["predicted_match"]), len(should_not_match)
    )

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "should_match_total": len(should_match),
        "recall_rate": recall_rate,
        "should_not_match_total": len(should_not_match),
        "false_positive_rate": false_positive_rate,
    }
