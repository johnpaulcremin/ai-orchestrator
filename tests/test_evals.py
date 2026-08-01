from __future__ import annotations

from collections import Counter

from evals.harness import evaluate, load_dataset, summarize, tier_from_mode_used
from evals.injection_harness import (
    ProbeResult,
    evaluate as inj_evaluate,
    load_dataset as inj_load_dataset,
    summarize as inj_summarize,
)
from evals.semantic_cache_harness import (
    evaluate as sc_evaluate,
    load_dataset as sc_load_dataset,
    summarize as sc_summarize,
)


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


# --- semantic-cache precision eval (evals/semantic_cache_harness.py) --------------


def _unit_vector_embedder(vectors: dict[str, list[float]]):
    def embed(text: str) -> list[float] | None:
        return vectors.get(text)

    return embed


def _real_cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def test_semantic_cache_eval_scores_a_true_paraphrase_as_a_hit() -> None:
    dataset = [{"stored": "s", "query": "q", "should_match": True}]
    vectors = {"s": [1.0, 0.0], "q": [1.0, 0.0]}  # identical -> similarity 1.0
    results = sc_evaluate(dataset, _unit_vector_embedder(vectors), _real_cosine, 0.96)
    assert results[0]["correct"] is True
    assert results[0]["predicted_match"] is True


def test_semantic_cache_eval_scores_a_near_miss_as_a_false_positive_when_over_threshold() -> (
    None
):
    dataset = [{"stored": "s", "query": "q", "should_match": False}]
    # High but not identical similarity, still clears a moderate threshold.
    vectors = {"s": [1.0, 0.0], "q": [0.99, 0.14]}
    results = sc_evaluate(dataset, _unit_vector_embedder(vectors), _real_cosine, 0.9)
    assert results[0]["predicted_match"] is True
    assert results[0]["expected_match"] is False
    assert results[0]["correct"] is False  # this is exactly the dangerous case


def test_semantic_cache_eval_treats_a_failed_embedding_as_no_match() -> None:
    dataset = [{"stored": "s", "query": "q", "should_match": True}]
    results = sc_evaluate(dataset, lambda _text: None, _real_cosine, 0.5)
    assert results[0]["similarity"] == 0.0
    assert results[0]["predicted_match"] is False
    assert results[0]["correct"] is False


def test_semantic_cache_summarize_reports_hit_rate_and_false_positive_rate_separately() -> (
    None
):
    results = [
        {"expected_match": True, "predicted_match": True, "correct": True},
        {
            "expected_match": True,
            "predicted_match": False,
            "correct": False,
        },  # a miss, not dangerous
        {
            "expected_match": False,
            "predicted_match": True,
            "correct": False,
        },  # a false positive
        {"expected_match": False, "predicted_match": False, "correct": True},
    ]
    summary = sc_summarize(results)
    assert summary["total"] == 4
    assert summary["correct"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["should_match_total"] == 2
    assert summary["hit_rate"] == 0.5  # 1 of 2 true paraphrases hit
    assert summary["should_not_match_total"] == 2
    assert summary["false_positive_rate"] == 0.5  # 1 of 2 near-misses wrongly matched


def test_semantic_cache_summarize_handles_an_all_hit_all_miss_dataset() -> None:
    results = [
        {"expected_match": True, "predicted_match": True, "correct": True},
        {"expected_match": False, "predicted_match": False, "correct": True},
    ]
    summary = sc_summarize(results)
    assert summary["accuracy"] == 1.0
    assert summary["hit_rate"] == 1.0
    assert summary["false_positive_rate"] == 0.0


def test_semantic_cache_bundled_dataset_is_well_formed_and_balanced() -> None:
    dataset = sc_load_dataset()
    assert len(dataset) >= 20
    should_match = [item for item in dataset if item["should_match"]]
    should_not_match = [item for item in dataset if not item["should_match"]]
    assert len(should_match) >= 10
    assert len(should_not_match) >= 10
    for item in dataset:
        assert item["stored"]
        assert item["query"]
        assert isinstance(item["should_match"], bool)


# --- injection eval harness (evals/injection_harness.py) ---------------------


def test_injection_evaluate_flags_compliance() -> None:
    dataset = [
        {
            "id": "leak",
            "forbidden_substrings": ["SECRET"],
            "forbidden_action": None,
            "injected_document": "...",
            "question": "...",
        }
    ]
    results = inj_evaluate(
        dataset,
        lambda _item: ProbeResult(answer="The SECRET is out.", proposed_action=None),
    )
    assert results[0]["complied"] is True
    assert results[0]["proposed_forbidden_action"] is False
    assert results[0]["safe"] is False


def test_injection_evaluate_flags_proposing_the_forbidden_action() -> None:
    dataset = [
        {
            "id": "propose",
            "forbidden_substrings": [],
            "forbidden_action": "send_email",
            "injected_document": "...",
            "question": "...",
        }
    ]
    results = inj_evaluate(
        dataset,
        lambda _item: ProbeResult(
            answer="Here is a summary.", proposed_action="send_email"
        ),
    )
    assert results[0]["complied"] is False
    assert results[0]["proposed_forbidden_action"] is True
    assert results[0]["safe"] is False


def test_injection_evaluate_safe_when_neither_happens() -> None:
    dataset = [
        {
            "id": "safe",
            "forbidden_substrings": ["SECRET"],
            "forbidden_action": "send_email",
            "injected_document": "...",
            "question": "...",
        }
    ]
    results = inj_evaluate(
        dataset,
        lambda _item: ProbeResult(
            answer="Here is a benign summary.", proposed_action=None
        ),
    )
    assert results[0]["safe"] is True


def test_injection_evaluate_an_unrelated_proposed_action_is_still_safe() -> None:
    """Proposing SOME action isn't automatically unsafe -- only proposing
    the SPECIFIC forbidden one from this dataset item is."""
    dataset = [
        {
            "id": "unrelated-action",
            "forbidden_substrings": [],
            "forbidden_action": "send_email",
            "injected_document": "...",
            "question": "...",
        }
    ]
    results = inj_evaluate(
        dataset,
        lambda _item: ProbeResult(answer="ok", proposed_action="update_sheet"),
    )
    assert results[0]["proposed_forbidden_action"] is False
    assert results[0]["safe"] is True


def test_injection_summarize_reports_the_safety_rate() -> None:
    results = [
        {
            "id": "a",
            "complied": False,
            "proposed_forbidden_action": False,
            "safe": True,
        },
        {
            "id": "b",
            "complied": True,
            "proposed_forbidden_action": False,
            "safe": False,
        },
    ]
    summary = inj_summarize(results)
    assert summary["total"] == 2
    assert summary["safe"] == 1
    assert summary["safety_rate"] == 0.5
    assert summary["complied_ids"] == ["b"]
    assert summary["proposed_forbidden_action_ids"] == []


def test_injection_summarize_empty_dataset_is_vacuously_safe() -> None:
    summary = inj_summarize([])
    assert summary["total"] == 0
    assert summary["safety_rate"] == 1.0


def test_injection_bundled_dataset_is_well_formed() -> None:
    dataset = inj_load_dataset()
    assert len(dataset) >= 3
    for item in dataset:
        assert item["id"]
        assert item["injected_document"]
        assert item["question"]
        assert item.get("forbidden_substrings") or item.get("forbidden_action")
