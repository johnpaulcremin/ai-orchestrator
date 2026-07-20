from __future__ import annotations

from collections import Counter

from evals.harness import evaluate, load_dataset, summarize, tier_from_mode_used


class _FakeDecision:
    def __init__(self, mode_used: str, model: str = "m", category: str = "") -> None:
        self.mode_used = mode_used
        self.model = model
        self.category = category


def test_tier_from_mode_used() -> None:
    assert tier_from_mode_used("auto->fast") == "fast"
    assert tier_from_mode_used("auto->smart") == "smart"
    assert tier_from_mode_used("smart") == "smart"
    assert tier_from_mode_used("fast") == "fast"
    assert tier_from_mode_used("") is None
    assert tier_from_mode_used("unknown") is None


def test_evaluate_and_summarize_tier_scoring() -> None:
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


def test_category_classification_scoring() -> None:
    dataset = [
        {"prompt": "a", "expected_tier": "smart", "category": "coding"},
        {"prompt": "b", "expected_tier": "smart", "category": "reasoning"},
        {"prompt": "c", "expected_tier": "smart", "category": "reasoning"},
    ]

    # a: right category; b: wrong category (still smart tier); c: no category (heuristic).
    predictions = {
        "a": _FakeDecision("auto->smart", category="coding"),
        "b": _FakeDecision("auto->smart", category="analysis"),
        "c": _FakeDecision("auto->smart", category=""),
    }
    results = evaluate(dataset, lambda q: predictions[q])

    assert [r["category_correct"] for r in results] == [True, False, False]

    summary = summarize(results)
    # Tier is right for all three, category only for one.
    assert summary["accuracy"] == 1.0
    assert summary["category_correct"] == 1
    assert abs(summary["category_accuracy"] - (1 / 3)) < 1e-9

    coding = summary["by_category"]["coding"]
    assert coding == {
        "total": 1,
        "tier_correct": 1,
        "tier_accuracy": 1.0,
        "category_correct": 1,
        "category_accuracy": 1.0,
    }
    reasoning = summary["by_category"]["reasoning"]
    assert reasoning["total"] == 2
    assert reasoning["tier_accuracy"] == 1.0
    assert reasoning["category_correct"] == 0
    assert reasoning["category_accuracy"] == 0.0


def test_overall_category_accuracy_denominator_is_total() -> None:
    # Construct a case where total (4), tier-correct (3) and non-empty-predicted
    # count (3) are all different, so this pins category_accuracy to /total and
    # would catch a denominator swap (e.g. /tier_correct = 1/3, not 1/4).
    dataset = [
        {"prompt": "a", "expected_tier": "smart", "category": "coding"},
        {"prompt": "b", "expected_tier": "smart", "category": "coding"},
        {"prompt": "c", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "d", "expected_tier": "smart", "category": "reasoning"},
    ]
    predictions = {
        "a": _FakeDecision("auto->smart", category="coding"),  # tier ok, cat ok
        "b": _FakeDecision("auto->smart", category="analysis"),  # tier ok, cat wrong
        "c": _FakeDecision("auto->smart", category="math"),  # tier WRONG, cat wrong
        "d": _FakeDecision("auto->smart", category=""),  # tier ok, cat empty (miss)
    }
    summary = summarize(evaluate(dataset, lambda q: predictions[q]))

    assert summary["total"] == 4
    assert summary["correct"] == 3  # tier: a, b, d
    assert summary["category_correct"] == 1  # only a
    assert abs(summary["category_accuracy"] - 0.25) < 1e-9  # 1/4, not 1/3
    assert abs(summary["accuracy"] - 0.75) < 1e-9


def test_empty_predicted_category_never_counts_as_correct() -> None:
    dataset = [{"prompt": "a", "expected_tier": "fast", "category": ""}]
    # Both expected and predicted category are empty — must not be a match.
    results = evaluate(dataset, lambda _q: _FakeDecision("auto->fast", category=""))
    assert results[0]["category_correct"] is False


def test_bundled_dataset_is_well_formed_and_balanced() -> None:
    dataset = load_dataset()
    assert len(dataset) >= 50
    for item in dataset:
        assert item["prompt"]
        assert item["expected_tier"] in {"fast", "smart"}
        assert item["category"]

    # All eleven categories present, each with several examples.
    from app.categories import ALL_CATEGORIES

    counts = Counter(item["category"] for item in dataset)
    assert set(counts) == set(ALL_CATEGORIES)
    assert min(counts.values()) >= 3
