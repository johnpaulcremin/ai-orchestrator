"""The golden answer-quality eval's pure logic (evals/golden_harness.py) —
check evaluation, item scoring, run summaries, and the drift comparison that
is the eval's actual product. All offline: the paid half lives in
evals/golden_run.py, which CI never runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.categories import ALL_CATEGORIES
from evals import golden_harness


# --- evaluate_checks ------------------------------------------------------------


def test_contains_is_case_insensitive() -> None:
    results = golden_harness.evaluate_checks(
        "The capital is CANBERRA.", [{"type": "contains", "value": "canberra"}]
    )
    assert results[0]["passed"] is True


def test_not_contains_flags_the_hedge() -> None:
    checks = [{"type": "not_contains", "value": "cannot be determined"}]
    assert golden_harness.evaluate_checks("It is Cara.", checks)[0]["passed"] is True
    assert (
        golden_harness.evaluate_checks("It cannot be determined.", checks)[0]["passed"]
        is False
    )


def test_regex_searches_case_insensitively() -> None:
    results = golden_harness.evaluate_checks(
        "1. do this\n2. then that", [{"type": "regex", "value": r"(^|\n)\s*2[.)]"}]
    )
    assert results[0]["passed"] is True


def test_any_of_needs_only_one() -> None:
    results = golden_harness.evaluate_checks(
        "use s[::-1]", [{"type": "any_of", "values": ["reversed(", "[::-1]"]}]
    )
    assert results[0]["passed"] is True


def test_empty_answer_fails_every_check_including_not_contains() -> None:
    """An empty answer trivially 'not containing' a hedge is not evidence of
    anything — a dead provider must not score partially correct."""
    checks = [
        {"type": "contains", "value": "x"},
        {"type": "not_contains", "value": "y"},
    ]
    for answer in ("", "   ", None):
        results = golden_harness.evaluate_checks(answer, checks)  # type: ignore[arg-type]
        assert [r["passed"] for r in results] == [False, False]


def test_unknown_check_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown check type"):
        golden_harness.evaluate_checks("text", [{"type": "llm_judge", "value": "?"}])


# --- score_item / summarize -----------------------------------------------------


def _item(item_id: str = "i1", category: str = "math") -> dict:
    return {
        "id": item_id,
        "category": category,
        "checks": [
            {"type": "contains", "value": "391"},
            {"type": "not_contains", "value": "cannot"},
        ],
    }


def test_score_item_requires_every_check() -> None:
    """Partially right is failed: drift is judged per item, and '2 of 3
    checks' is exactly the ambiguity deterministic checks exist to avoid."""
    good = golden_harness.score_item(_item(), "It is 391.", "auto->smart:math", 0.01)
    partial = golden_harness.score_item(
        _item(), "It is 391 but cannot be sure.", "auto->smart:math", 0.01
    )
    assert good["passed"] is True and good["failed_checks"] == []
    assert partial["passed"] is False
    assert partial["failed_checks"] == [{"type": "not_contains", "value": "cannot"}]


def test_summarize_counts_and_costs() -> None:
    results = [
        golden_harness.score_item(_item("a"), "391", "m1", 0.01),
        golden_harness.score_item(_item("b", "coding"), "nope", "m2", None),
    ]
    summary = golden_harness.summarize(results)
    assert summary["total"] == 2 and summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["by_category"] == {
        "math": {"passed": 1, "total": 1},
        "coding": {"passed": 0, "total": 1},
    }
    assert summary["total_cost_usd"] == 0.01  # None cost is not 0-summed blindly
    assert summary["failed_ids"] == ["b"]


# --- compare_runs: the drift report ---------------------------------------------


def _row(item_id: str, passed: bool, mode: str = "auto->fast:quick_fact") -> dict:
    return {
        "id": item_id,
        "category": "quick_fact",
        "passed": passed,
        "failed_checks": [],
        "mode_used": mode,
        "cost_usd": 0.0,
        "answer_chars": 10,
    }


def test_compare_runs_reports_regressions_fixes_and_model_changes() -> None:
    previous = [_row("a", True), _row("b", False), _row("c", True, "auto->fast:m-old")]
    current = [_row("a", False), _row("b", True), _row("c", True, "auto->fast:m-new")]
    drift = golden_harness.compare_runs(previous, current)
    assert drift["regressions"] == ["a"]
    assert drift["fixes"] == ["b"]
    # Reported even though c passed both times: "still right, different
    # model" is the quiet provider drift worth noticing before quality moves.
    assert drift["model_changes"] == [
        {"id": "c", "from": "auto->fast:m-old", "to": "auto->fast:m-new"}
    ]


def test_compare_runs_lists_dataset_membership_changes_without_guessing() -> None:
    drift = golden_harness.compare_runs([_row("old", True)], [_row("new", True)])
    assert drift["only_in_previous"] == ["old"]
    assert drift["only_in_current"] == ["new"]
    assert drift["regressions"] == [] and drift["fixes"] == []


# --- dataset hygiene ------------------------------------------------------------


def test_real_dataset_loads_and_is_well_formed() -> None:
    items = golden_harness.load_dataset()
    assert len(items) >= 10
    for item in items:
        assert item["category"] in ALL_CATEGORIES, item["id"]
        assert item["checks"], item["id"]


def test_real_dataset_covers_every_category() -> None:
    """The point is drift across the whole routing surface — a category with
    no golden prompt is a blind spot someone removed without noticing."""
    covered = {item["category"] for item in golden_harness.load_dataset()}
    assert covered == set(ALL_CATEGORIES)


def test_load_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    bad = tmp_path / "dup.json"
    bad.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "x",
                        "category": "math",
                        "checks": [{"type": "contains", "value": "1"}],
                    },
                    {
                        "id": "x",
                        "category": "math",
                        "checks": [{"type": "contains", "value": "2"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        golden_harness.load_dataset(bad)


def test_load_dataset_rejects_a_checkless_item(tmp_path: Path) -> None:
    bad = tmp_path / "nochecks.json"
    bad.write_text(
        json.dumps({"items": [{"id": "x", "category": "math", "checks": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no checks"):
        golden_harness.load_dataset(bad)


def test_latest_results_file_picks_the_newest_by_name(tmp_path: Path) -> None:
    assert golden_harness.latest_results_file(tmp_path / "missing") is None
    (tmp_path / "golden-20260810-120000.json").write_text("{}", encoding="utf-8")
    (tmp_path / "golden-20260811-090000.json").write_text("{}", encoding="utf-8")
    newest = golden_harness.latest_results_file(tmp_path)
    assert newest is not None and newest.name == "golden-20260811-090000.json"
