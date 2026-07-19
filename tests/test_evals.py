from __future__ import annotations

from evals.harness import evaluate, load_dataset, summarize, tier_from_mode_used


class _FakeDecision:
    def __init__(self, mode_used: str, model: str = "m") -> None:
        self.mode_used = mode_used
        self.model = model


def test_tier_from_mode_used() -> None:
    assert tier_from_mode_used("auto->fast") == "fast"
    assert tier_from_mode_used("auto->smart") == "smart"
    assert tier_from_mode_used("smart") == "smart"
    assert tier_from_mode_used("fast") == "fast"
    assert tier_from_mode_used("") is None
    assert tier_from_mode_used("unknown") is None


def test_evaluate_and_summarize_scoring() -> None:
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "smart", "category": "coding"},
    ]

    # Always predicts fast: item a correct, item b wrong.
    results = evaluate(dataset, lambda _q: _FakeDecision("auto->fast"))

    assert results[0]["correct"] is True
    assert results[1]["correct"] is False

    summary = summarize(results)
    assert summary["total"] == 2
    assert summary["correct"] == 1
    assert abs(summary["accuracy"] - 0.5) < 1e-9
    assert summary["confusion"]["smart->fast"] == 1


def test_bundled_dataset_is_well_formed() -> None:
    dataset = load_dataset()
    assert len(dataset) >= 20
    for item in dataset:
        assert item["prompt"]
        assert item["expected_tier"] in {"fast", "smart"}
        assert item["category"]
