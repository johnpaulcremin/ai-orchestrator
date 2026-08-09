from __future__ import annotations

from collections import Counter

from evals.harness import (
    UNPARSED_TIER,
    evaluate,
    load_dataset,
    summarize,
    tier_from_mode_used,
)
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
from evals.memory_harness import (
    evaluate as mem_evaluate,
    load_dataset as mem_load_dataset,
    summarize as mem_summarize,
)
from evals.separability import ceiling
from evals.multipart_harness import evaluate as multipart_evaluate
from evals.multipart_harness import load_dataset as multipart_load_dataset
from evals.multipart_harness import summarize as multipart_summarize
from evals.self_describe_harness import (
    evaluate as sd_evaluate,
    load_dataset as sd_load_dataset,
    summarize as sd_summarize,
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


def test_evaluate_and_summarize_never_crash_on_unparseable_mode_used() -> None:
    """Regression pin for a real crash: a live run can return mode_used
    that doesn't map to either tier (e.g. free-lane routing's
    "auto->free:<model>" -- see app/free_tier.py), which
    tier_from_mode_used maps to None. summarize()'s confusion-key sort
    used to crash comparing None against a string the first time this
    happened for real; both directions (predicted AND, defensively,
    expected) must bucket to UNPARSED_TIER instead."""
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "smart", "category": "coding"},
    ]
    decisions = {
        "a": _FakeDecision("auto->free:groq/llama-3"),  # unparseable mode_used
        "b": _FakeDecision("auto->smart"),
    }
    results = evaluate(dataset, lambda q: decisions[q])

    assert results[0]["predicted"] == UNPARSED_TIER
    assert results[0]["mode_used"] == "auto->free:groq/llama-3"
    assert results[0]["correct"] is False
    assert results[1]["predicted"] == "smart"

    summary = summarize(results)  # must not raise
    assert summary["total"] == 2
    assert summary["correct"] == 1
    assert summary["confusion"][f"fast->{UNPARSED_TIER}"] == 1
    assert summary["confusion"]["smart->smart"] == 1


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
        # No budget/free lane in play here, so nothing is excluded and the
        # achievable figures match the raw ones (see the dedicated
        # per-category ceiling test below).
        "tier_unscoreable": 0,
        "tier_achievable": 1,
        "tier_achievable_accuracy": 1.0,
        "category_correct": 1,
        "category_accuracy": 1.0,
    }
    reasoning = summary["by_category"]["reasoning"]
    assert reasoning["total"] == 2
    assert reasoning["tier_accuracy"] == 1.0
    assert reasoning["category_correct"] == 0
    assert reasoning["category_accuracy"] == 0.0


def test_budget_routed_items_are_excluded_from_the_tier_ceiling() -> None:
    """The reporting fix: with a budget tier configured, a low-complexity
    fast-category prompt routes to auto->budget, which a fast/smart dataset
    has no label for. Counting those as failures made a perfect run read as
    50% and cost a day of suspicion — they are unscoreable BY CONSTRUCTION,
    so the ceiling has to move with the configuration."""
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "fast", "category": "casual_chat"},
        {"prompt": "c", "expected_tier": "smart", "category": "coding"},
        {"prompt": "d", "expected_tier": "smart", "category": "reasoning"},
    ]
    predictions = {
        "a": _FakeDecision("auto->budget", category="quick_fact"),
        "b": _FakeDecision("auto->budget", category="casual_chat"),
        "c": _FakeDecision("auto->smart", category="coding"),
        "d": _FakeDecision("auto->smart", category="reasoning"),
    }
    results = evaluate(dataset, lambda q: predictions[q])
    assert [r["unscoreable_lane"] for r in results] == ["budget", "budget", None, None]

    summary = summarize(results)
    # Raw is unchanged — it is still what actually happened.
    assert summary["correct"] == 2
    assert abs(summary["accuracy"] - 0.5) < 1e-9
    # ...but only 2 of the 4 were gradeable, and both were graded right.
    assert summary["tier_unscoreable"] == 2
    assert summary["tier_unscoreable_by_lane"] == {"budget": 2}
    assert summary["tier_achievable"] == 2
    assert abs(summary["tier_ceiling"] - 0.5) < 1e-9
    assert summary["tier_achievable_accuracy"] == 1.0


def test_free_lane_items_are_excluded_too() -> None:
    """The other config-enabled lane (FREE_TIER_ROUTING) gets the identical
    treatment — the exclusion is per named lane, not budget-specific."""
    dataset = [{"prompt": "a", "expected_tier": "fast", "category": "quick_fact"}]
    results = evaluate(
        dataset,
        lambda _q: _FakeDecision("auto->free:groq/llama-3", category="quick_fact"),
    )
    summary = summarize(results)
    assert summary["tier_unscoreable_by_lane"] == {"free": 1}
    assert summary["tier_achievable"] == 0
    # No gradeable item left: 0.0, never a divide-by-zero and never a
    # flattering 100% off an empty denominator.
    assert summary["tier_achievable_accuracy"] == 0.0


def test_an_unparsed_result_with_no_known_lane_still_counts_against_the_score() -> None:
    """The exclusion must be EARNED by a named lane. Something new and
    unrecognised (here auto->clarify) is a real finding, and quietly
    excusing it would be exactly the blind spot this reporting exists to
    remove — in the opposite direction."""
    dataset = [{"prompt": "a", "expected_tier": "fast", "category": "quick_fact"}]
    results = evaluate(dataset, lambda _q: _FakeDecision("auto->clarify"))

    assert results[0]["predicted"] == UNPARSED_TIER
    assert results[0]["unscoreable_lane"] is None

    summary = summarize(results)
    assert summary["tier_unscoreable"] == 0
    assert summary["tier_achievable"] == 1
    assert summary["tier_ceiling"] == 1.0
    assert summary["tier_achievable_accuracy"] == 0.0


def test_a_smart_expected_item_in_a_cheap_lane_is_surfaced_not_buried() -> None:
    """Excluding an item from the denominator could hide a genuine misroute:
    a smart-expected prompt landing in the budget lane is a real problem.
    summarize breaks the exclusions down by expected tier so run.py can warn
    rather than launder it into a better-looking score."""
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "smart", "category": "coding"},
    ]
    results = evaluate(dataset, lambda _q: _FakeDecision("auto->budget"))
    summary = summarize(results)
    assert summary["tier_unscoreable_by_expected_tier"] == {"fast": 1, "smart": 1}


def test_category_ceiling_moves_with_the_prefilter() -> None:
    """Category accuracy has its own config-moving ceiling: an item the
    classifier never saw (ROUTER_PREFILTER's shortcut, or the heuristic
    fallback) has no predicted category to grade."""
    dataset = [
        {"prompt": "a", "expected_tier": "smart", "category": "coding"},
        {"prompt": "b", "expected_tier": "fast", "category": "casual_chat"},
    ]
    predictions = {
        "a": _FakeDecision("auto->smart", category="coding"),
        "b": _FakeDecision("auto->fast", category=""),  # prefiltered: no category
    }
    summary = summarize(evaluate(dataset, lambda q: predictions[q]))

    assert abs(summary["category_accuracy"] - 0.5) < 1e-9  # raw, unchanged
    assert summary["category_unscoreable"] == 1
    assert summary["category_achievable"] == 1
    assert abs(summary["category_ceiling"] - 0.5) < 1e-9
    assert summary["category_achievable_accuracy"] == 1.0


def test_per_category_rows_carry_their_own_ceiling() -> None:
    """The per-category table is where someone looks after a low headline, so
    it must not repeat the trap: with the budget tier on, exactly the fast
    categories show 0% raw tier accuracy."""
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "fast", "category": "quick_fact"},
    ]
    predictions = {
        "a": _FakeDecision("auto->budget", category="quick_fact"),
        "b": _FakeDecision("auto->fast", category="quick_fact"),
    }
    summary = summarize(evaluate(dataset, lambda q: predictions[q]))

    row = summary["by_category"]["quick_fact"]
    assert abs(row["tier_accuracy"] - 0.5) < 1e-9  # raw: 1 of 2
    assert row["tier_unscoreable"] == 1
    assert row["tier_achievable"] == 1
    assert row["tier_achievable_accuracy"] == 1.0  # the one gradeable item was right


def test_ceilings_are_full_when_no_lane_or_prefilter_is_in_play() -> None:
    """With neither a budget/free lane nor an unclassified item, both ceilings
    are 100% and the achievable figures equal the raw ones — so the added
    reporting is inert on a plain configuration."""
    dataset = [
        {"prompt": "a", "expected_tier": "fast", "category": "quick_fact"},
        {"prompt": "b", "expected_tier": "smart", "category": "coding"},
    ]
    predictions = {
        "a": _FakeDecision("auto->fast", category="quick_fact"),
        "b": _FakeDecision("auto->fast", category="coding"),  # a real misroute
    }
    summary = summarize(evaluate(dataset, lambda q: predictions[q]))

    assert summary["tier_unscoreable"] == 0
    assert summary["tier_ceiling"] == 1.0
    assert summary["tier_achievable_accuracy"] == summary["accuracy"]
    assert summary["category_unscoreable"] == 0
    assert summary["category_ceiling"] == 1.0
    assert summary["category_achievable_accuracy"] == summary["category_accuracy"]


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
    # `similarity` mirrors what evaluate() really emits (and what the
    # separability ceiling reads); the four rows below are ordered so a trap
    # outscores a genuine paraphrase, i.e. the overlapping shape both real
    # datasets have.
    results = [
        {
            "similarity": 0.99,
            "expected_match": True,
            "predicted_match": True,
            "correct": True,
        },
        {
            "similarity": 0.80,
            "expected_match": True,
            "predicted_match": False,
            "correct": False,
        },  # a miss, not dangerous
        {
            "similarity": 0.97,
            "expected_match": False,
            "predicted_match": True,
            "correct": False,
        },  # a false positive
        {
            "similarity": 0.40,
            "expected_match": False,
            "predicted_match": False,
            "correct": True,
        },
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
        {
            "similarity": 0.99,
            "expected_match": True,
            "predicted_match": True,
            "correct": True,
        },
        {
            "similarity": 0.40,
            "expected_match": False,
            "predicted_match": False,
            "correct": True,
        },
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


# --- cross-conversation-memory precision eval (evals/memory_harness.py) -----


def test_memory_eval_scores_a_relevant_pair_as_a_hit() -> None:
    dataset = [{"stored": "s", "query": "q", "should_match": True}]
    vectors = {"s": [1.0, 0.0], "q": [1.0, 0.0]}  # identical -> similarity 1.0
    results = mem_evaluate(dataset, _unit_vector_embedder(vectors), _real_cosine, 0.75)
    assert results[0]["correct"] is True
    assert results[0]["predicted_match"] is True


def test_memory_eval_scores_an_unrelated_pair_as_a_false_positive_when_over_threshold() -> (
    None
):
    dataset = [{"stored": "s", "query": "q", "should_match": False}]
    vectors = {"s": [1.0, 0.0], "q": [0.9, 0.44]}  # ~0.9 similarity
    results = mem_evaluate(dataset, _unit_vector_embedder(vectors), _real_cosine, 0.75)
    assert results[0]["predicted_match"] is True
    assert results[0]["expected_match"] is False
    assert results[0]["correct"] is False


def test_memory_eval_treats_a_failed_embedding_as_no_match() -> None:
    dataset = [{"stored": "s", "query": "q", "should_match": True}]
    results = mem_evaluate(dataset, lambda _text: None, _real_cosine, 0.5)
    assert results[0]["similarity"] == 0.0
    assert results[0]["predicted_match"] is False
    assert results[0]["correct"] is False


def test_memory_summarize_reports_recall_rate_and_false_positive_rate_separately() -> (
    None
):
    # `similarity` mirrors what evaluate() really emits -- see the
    # semantic-cache twin above for why the fixture carries it.
    results = [
        {
            "similarity": 0.90,
            "expected_match": True,
            "predicted_match": True,
            "correct": True,
        },
        {
            "similarity": 0.70,
            "expected_match": True,
            "predicted_match": False,
            "correct": False,
        },
        {
            "similarity": 0.85,
            "expected_match": False,
            "predicted_match": True,
            "correct": False,
        },
        {
            "similarity": 0.40,
            "expected_match": False,
            "predicted_match": False,
            "correct": True,
        },
    ]
    summary = mem_summarize(results)
    assert summary["total"] == 4
    assert summary["correct"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["should_match_total"] == 2
    assert summary["recall_rate"] == 0.5
    assert summary["should_not_match_total"] == 2
    assert summary["false_positive_rate"] == 0.5


def test_memory_bundled_dataset_is_well_formed_and_balanced() -> None:
    dataset = mem_load_dataset()
    assert len(dataset) >= 10
    should_match = [item for item in dataset if item["should_match"]]
    should_not_match = [item for item in dataset if not item["should_match"]]
    assert len(should_match) >= 5
    assert len(should_not_match) >= 5
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


# --- SELF_DESCRIBE trigger-accuracy eval harness (evals/self_describe_harness.py) --


def test_self_describe_evaluate_marks_a_correct_fire() -> None:
    dataset = [{"id": "a", "question": "what models do you use?", "should_fire": True}]
    results = sd_evaluate(dataset, lambda _item: True)
    assert results[0] == {
        "id": "a",
        "question": "what models do you use?",
        "should_fire": True,
        "fired": True,
        "correct": True,
    }


def test_self_describe_evaluate_marks_a_false_positive() -> None:
    """The headline failure mode: firing on a trap."""
    dataset = [{"id": "trap", "question": "why did it fail?", "should_fire": False}]
    results = sd_evaluate(dataset, lambda _item: True)
    assert results[0]["fired"] is True
    assert results[0]["correct"] is False


def test_self_describe_evaluate_marks_a_false_negative() -> None:
    dataset = [{"id": "miss", "question": "what can you do?", "should_fire": True}]
    results = sd_evaluate(dataset, lambda _item: False)
    assert results[0]["fired"] is False
    assert results[0]["correct"] is False


def test_self_describe_summarize_reports_both_error_directions() -> None:
    results = [
        {"id": "hit", "should_fire": True, "fired": True, "correct": True},
        {"id": "miss", "should_fire": True, "fired": False, "correct": False},
        {"id": "clean", "should_fire": False, "fired": False, "correct": True},
        {"id": "misfire", "should_fire": False, "fired": True, "correct": False},
    ]
    summary = sd_summarize(results)
    assert summary["total"] == 4
    assert summary["correct"] == 2
    assert summary["accuracy"] == 0.5
    assert summary["should_fire_total"] == 2
    assert summary["false_negative_rate"] == 0.5
    assert summary["false_negative_ids"] == ["miss"]
    assert summary["should_not_fire_total"] == 2
    assert summary["false_positive_rate"] == 0.5
    assert summary["false_positive_ids"] == ["misfire"]


def test_self_describe_summarize_empty_dataset_reports_zero_rates() -> None:
    summary = sd_summarize([])
    assert summary["total"] == 0
    assert summary["false_positive_rate"] == 0.0
    assert summary["false_negative_rate"] == 0.0


def test_self_describe_bundled_dataset_is_well_formed() -> None:
    dataset = sd_load_dataset()
    should_fire = [item for item in dataset if item["should_fire"]]
    should_not_fire = [item for item in dataset if not item["should_fire"]]
    # Both directions genuinely represented -- a dataset all of one kind
    # would silently stop testing the other failure mode entirely.
    assert len(should_fire) >= 5
    assert len(should_not_fire) >= 5
    for item in dataset:
        assert item["id"]
        assert item["question"]
        assert isinstance(item["should_fire"], bool)


def test_self_describe_bundled_dataset_covers_the_documented_trap_categories() -> None:
    """Pins that the dataset actually exercises the trap categories named in
    self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION's negative guidance and
    the removed-phrase audit — not just "some" must-not-fire questions."""
    dataset = sd_load_dataset()
    ids = {item["id"] for item in dataset if not item["should_fire"]}
    has_prior_exchange_trap = any(
        item.get("prior_exchange") for item in dataset if not item["should_fire"]
    )
    assert has_prior_exchange_trap  # meta-question-about-a-prior-answer category
    assert any("general-ai" in i for i in ids)  # general AI question category
    assert any(
        "what-are-you" in i or "version-of" in i or "do-you-support" in i for i in ids
    )


# --- AUTO_WORKFLOW multi-artefact eval harness (evals/multipart_harness.py) ----


def test_multipart_evaluate_scores_both_directions() -> None:
    dataset = [
        {"id": "a", "prompt": "summary and spreadsheet", "should_fire": True},
        {"id": "b", "prompt": "compare X and Y", "should_fire": False},
    ]
    results = multipart_evaluate(dataset, lambda item: item["id"] == "a")
    assert [r["correct"] for r in results] == [True, True]


def test_multipart_summarize_separates_the_two_failure_directions() -> None:
    """They are not symmetric: a false positive turns an ordinary question
    into several model calls, while a false negative just leaves it on the
    single-shot path it used before this feature existed."""
    results = [
        {"id": "fp", "should_fire": False, "fired": True, "correct": False},
        {"id": "fn", "should_fire": True, "fired": False, "correct": False},
        {"id": "ok-fire", "should_fire": True, "fired": True, "correct": True},
        {"id": "ok-hold", "should_fire": False, "fired": False, "correct": True},
    ]
    summary = multipart_summarize(results)

    assert summary["accuracy"] == 0.5
    assert summary["false_positive_rate"] == 0.5
    assert summary["false_negative_rate"] == 0.5
    assert summary["false_positive_ids"] == ["fp"]
    assert summary["false_negative_ids"] == ["fn"]


def test_multipart_summarize_empty_dataset_reports_zero_rates() -> None:
    summary = multipart_summarize([])
    assert summary["total"] == 0
    assert summary["false_positive_rate"] == 0.0
    assert summary["false_negative_rate"] == 0.0


def test_multipart_dataset_covers_both_sides_of_the_line() -> None:
    """The whole point of the fixture set: a dataset with only should-fire
    cases could not detect the failure that actually costs money."""
    dataset = multipart_load_dataset()
    fire = [d for d in dataset if d["should_fire"]]
    hold = [d for d in dataset if not d["should_fire"]]

    assert len(fire) >= 5
    # More traps than positives, matching where the cost actually is.
    assert len(hold) > len(fire)
    assert len({d["id"] for d in dataset}) == len(dataset)  # ids unique


# --- separability ceiling (semantic cache + memory) ---------------------------
#
# A DIFFERENT ceiling from evals/run.py's: there, items are unscoreable by
# construction and leave the denominator. Here every item is scored and the
# limit is that the fixtures overlap, so some pair is wrong at any threshold.


def _pair(similarity: float, expected_match: bool, threshold: float) -> dict:
    predicted = similarity >= threshold
    return {
        "similarity": similarity,
        "expected_match": expected_match,
        "predicted_match": predicted,
        "correct": predicted == expected_match,
    }


def test_ceiling_is_100_percent_when_the_distributions_separate() -> None:
    """Control: with a clean gap, the ceiling must not claim a limit that
    isn't there — otherwise it would excuse real failures."""
    results = [_pair(0.95, True, 0.9), _pair(0.40, False, 0.9)]
    info = ceiling(results)
    assert info["overlaps"] is False
    assert info["best_accuracy"] == 1.0
    assert info["zero_fp_best_accuracy"] == 1.0


def test_ceiling_detects_overlap_and_caps_below_100() -> None:
    """The real shape of both datasets: a trap scoring above a genuine match
    means no threshold gets everything right."""
    results = [
        _pair(0.99, True, 0.96),
        _pair(0.93, False, 0.96),  # trap ABOVE the genuine pair below it
        _pair(0.88, True, 0.96),
    ]
    info = ceiling(results)
    assert info["overlaps"] is True
    assert info["highest_trap"] == 0.93
    assert info["lowest_true_match"] == 0.88
    assert info["best_correct"] == 2  # never 3, at any threshold
    assert info["best_accuracy"] < 1.0


def test_zero_false_positive_ceiling_is_reported_separately() -> None:
    """The stricter bound an operator actually lives within: for the semantic
    cache a false positive serves a confidently wrong answer, so "best
    accuracy" and "best accuracy without ever false-positiving" are different
    numbers and both are printed."""
    results = [
        _pair(0.99, True, 0.96),
        _pair(0.93, False, 0.96),
        _pair(0.88, True, 0.96),
        _pair(0.80, True, 0.96),
    ]
    info = ceiling(results)
    # Above the 0.93 trap: only the 0.99 pair matches -> 1 hit + 1 trap correct.
    assert info["zero_fp_best_accuracy"] < info["best_accuracy"]
    assert info["zero_fp_best_threshold"] > 0.93


def test_ceiling_survives_an_all_traps_dataset() -> None:
    """No genuine matches at all: no divide-by-zero, and `overlaps` must be
    False rather than crashing on a missing side."""
    info = ceiling([_pair(0.5, False, 0.9), _pair(0.2, False, 0.9)])
    assert info["overlaps"] is False
    assert info["lowest_true_match"] is None
    assert info["best_accuracy"] == 1.0


def test_ceiling_of_an_empty_result_set_is_not_a_crash() -> None:
    info = ceiling([])
    assert info["best_accuracy"] == 0.0
    assert info["best_threshold"] is None


def test_both_harness_summaries_carry_the_ceiling() -> None:
    """Wiring pin: each summarize() must expose it, or the CLIs print nothing
    and the misreading this exists to stop comes straight back."""
    results = [_pair(0.99, True, 0.96), _pair(0.50, False, 0.96)]
    assert "ceiling" in sc_summarize(results)
    assert "ceiling" in mem_summarize(results)


# --- the heuristic fallback, scored offline against the real dataset -----------
#
# The routing eval proper (evals/run.py) makes real classifier calls and needs an
# API key, so it cannot gate anything here. The HEURISTIC FALLBACK can: it is
# pure keyword matching, so decide_route(..., client=None) scores all 55 prompts
# with no network at all. That makes the fallback's accuracy the one part of
# routing quality this suite can hold a floor under.
#
# Worth being exact about what this measures: the fallback that runs only when
# the classifier is unavailable, NOT the shipped AI-classifier accuracy. It is a
# floor on the outage path.


def _score_heuristic() -> dict[str, object]:
    from app.routing import decide_route
    from app.schemas import Mode

    from evals.harness import evaluate, load_dataset, summarize

    return summarize(
        evaluate(load_dataset(), lambda p: decide_route(p, Mode.auto, client=None))
    )


def test_the_heuristic_fallback_does_not_under_escalate_smart_work() -> None:
    """The regression this pins. The fallback used to own a tier policy with no
    notion of category and put 19 of 35 smart-expected prompts on the fast tier
    — during a classifier outage, the majority of hard questions silently got the
    cheap model and its 1500-token cap. It now applies the same
    `category in SMART_CATEGORIES` rule the classifier's output goes through.

    Measured floors, which only ever ratchet up (same convention as the coverage
    gates in pyproject.toml). If a change improves these, move them; if it
    worsens them, that is the finding.
    """
    summary = _score_heuristic()

    assert summary["correct"] >= 52, (
        f"tier accuracy fell to {summary['correct']}/55 (was 52/55 = 94.5%)"
    )
    under_escalated = summary["confusion"].get("smart->fast", 0)
    assert under_escalated <= 2, (
        f"{under_escalated} smart-expected prompts routed fast (was 2, originally 19)"
    )


def test_the_heuristic_fallback_does_not_escalate_everything() -> None:
    """The counterweight, and the reason the number above is worth anything: the
    tier score must not be bought by sending everything to the dear tier. Only
    one fast-expected prompt over-escalates, unchanged by the fix."""
    summary = _score_heuristic()

    over_escalated = summary["confusion"].get("fast->smart", 0)
    assert over_escalated <= 1, (
        f"{over_escalated} fast-expected prompts routed smart (was 1)"
    )


def test_the_heuristic_fallback_now_predicts_a_category_at_all() -> None:
    """Category accuracy used to be 0/55 with a ceiling of 0/55 — not a failure,
    but literally unscoreable, because the fallback produced no category to
    grade. It produces one for most prompts now, which is also what lets the
    orchestrator's category-gated behaviour work during an outage."""
    summary = _score_heuristic()

    assert summary["category_achievable"] >= 39, (
        "the fallback stopped predicting categories for most prompts"
    )
    assert summary["category_correct"] >= 36, (
        f"category accuracy fell to {summary['category_correct']}/55 (was 36/55)"
    )
