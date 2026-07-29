"""Pure scoring logic for the semantic-cache precision eval — see
evals/semantic_cache_run.py for the CLI that drives it with real embeddings.

Distinct from evals/harness.py's routing eval: a wrong routing decision costs
money or quality, but a wrong semantic-cache MATCH means the app can serve a
confidently-wrong cached answer to a genuinely different question — the one
new failure mode semantic caching introduced that isn't caught by the routing
eval or by test_semantic_cache.py's unit tests (which stub embeddings rather
than measuring real embedding-model behavior against a threshold).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "semantic_cache_dataset.json"

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
    matched at `threshold` (see semantic_cache.threshold()) against whether
    it SHOULD have (should_match in the dataset)."""
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

    # hit_rate: of the genuine paraphrases, how many actually cleared the
    # threshold — a miss here just costs one ordinary model call (ok).
    hit_rate = _rate(
        sum(1 for r in should_match if r["predicted_match"]), len(should_match)
    )
    # false_positive_rate: of the near-misses, how many WRONGLY cleared the
    # threshold — this is the dangerous direction (a confidently wrong cached
    # answer served for a different question), so this is the number to watch.
    false_positive_rate = _rate(
        sum(1 for r in should_not_match if r["predicted_match"]), len(should_not_match)
    )

    return {
        "total": total,
        "correct": correct,
        "accuracy": _rate(correct, total),
        "should_match_total": len(should_match),
        "hit_rate": hit_rate,
        "should_not_match_total": len(should_not_match),
        "false_positive_rate": false_positive_rate,
    }
