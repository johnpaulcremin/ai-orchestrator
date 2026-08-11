"""Pure scoring and drift logic for the golden answer-quality eval — no
model calls, no I/O beyond reading the dataset, so tests/test_golden_eval.py
exercises all of it offline.

Same split as harness.py (the routing eval): the runner (golden_run.py) does
the paid part and hands plain data in; everything judgeable is judged here.

The checks are deterministic FLOORS, not judgments: substring/regex facts a
correct answer will contain. A right answer phrased unusually can fail one —
acceptable, because the signal is DRIFT: the same checks against the same
prompts over time. compare_runs() is the actual product; a single run's pass
rate is just its baseline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DATASET_PATH = Path(__file__).with_name("golden_dataset.json")

# Where golden_run.py persists each run's results, one JSON file per run,
# named golden-<UTC timestamp>.json so lexicographic order is chronological
# order. Gitignored: results are per-deployment measurements (your models,
# your keys, your latencies), not repository artifacts.
RESULTS_DIR = Path(__file__).with_name("results")


def load_dataset(path: Path | None = None) -> list[dict[str, Any]]:
    raw = json.loads((path or _DATASET_PATH).read_text(encoding="utf-8"))
    items = raw["items"]
    seen: set[str] = set()
    for item in items:
        if item["id"] in seen:
            raise ValueError(f"duplicate golden item id: {item['id']}")
        seen.add(item["id"])
        if not item.get("checks"):
            raise ValueError(f"golden item {item['id']} has no checks")
    return items


def evaluate_checks(answer: str, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Judge one answer against its checks: [{check, passed}] in order.

    An empty/None answer fails every positive check and — deliberately —
    also fails not_contains: an empty answer trivially "not containing" a
    hedge is not evidence of anything, and letting it pass would score a
    dead provider as partially correct.
    """
    text = (answer or "").strip()
    results: list[dict[str, Any]] = []
    for check in checks:
        kind = check["type"]
        if not text:
            passed = False
        elif kind == "contains":
            passed = str(check["value"]).lower() in text.lower()
        elif kind == "not_contains":
            passed = str(check["value"]).lower() not in text.lower()
        elif kind == "regex":
            passed = re.search(str(check["value"]), text, re.IGNORECASE) is not None
        elif kind == "any_of":
            passed = any(str(v).lower() in text.lower() for v in check["values"])
        else:
            raise ValueError(f"unknown check type: {kind}")
        results.append({"check": check, "passed": passed})
    return results


def score_item(
    item: dict[str, Any], answer: str, mode_used: str, cost_usd: float | None
) -> dict[str, Any]:
    """One item's persisted result row. `passed` is all-checks-passed —
    a partially right answer is a failed item, since drift is judged on the
    item level and "2 of 3 checks" is exactly the ambiguity checks exist to
    avoid."""
    check_results = evaluate_checks(answer, item["checks"])
    return {
        "id": item["id"],
        "category": item["category"],
        "passed": all(r["passed"] for r in check_results),
        "failed_checks": [r["check"] for r in check_results if not r["passed"]],
        "mode_used": mode_used,
        "cost_usd": cost_usd,
        "answer_chars": len((answer or "").strip()),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        slot = by_category.setdefault(r["category"], {"passed": 0, "total": 0})
        slot["total"] += 1
        slot["passed"] += int(r["passed"])
    costs = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    return {
        "total": total,
        "passed": passed,
        "pass_rate": (passed / total) if total else None,
        "by_category": by_category,
        "total_cost_usd": sum(costs) if costs else 0.0,
        "failed_ids": sorted(r["id"] for r in results if not r["passed"]),
    }


def compare_runs(
    previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> dict[str, Any]:
    """The drift report between two runs' result rows — the reason this eval
    exists. Items only in one run are listed, not guessed about; a model
    change for an item is reported even when both runs passed, since "still
    right, but a different model answered" is exactly the quiet provider
    drift worth noticing before quality moves."""
    prev_by_id = {r["id"]: r for r in previous}
    cur_by_id = {r["id"]: r for r in current}
    shared = sorted(prev_by_id.keys() & cur_by_id.keys())
    regressions = [
        i for i in shared if prev_by_id[i]["passed"] and not cur_by_id[i]["passed"]
    ]
    fixes = [
        i for i in shared if not prev_by_id[i]["passed"] and cur_by_id[i]["passed"]
    ]
    model_changes = [
        {
            "id": i,
            "from": prev_by_id[i]["mode_used"],
            "to": cur_by_id[i]["mode_used"],
        }
        for i in shared
        if prev_by_id[i]["mode_used"] != cur_by_id[i]["mode_used"]
    ]
    return {
        "regressions": regressions,
        "fixes": fixes,
        "model_changes": model_changes,
        "only_in_previous": sorted(prev_by_id.keys() - cur_by_id.keys()),
        "only_in_current": sorted(cur_by_id.keys() - prev_by_id.keys()),
    }


def latest_results_file(results_dir: Path | None = None) -> Path | None:
    """The most recent persisted run, by filename (timestamps sort
    lexicographically), or None on a first run."""
    directory = results_dir or RESULTS_DIR
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("golden-*.json"))
    return files[-1] if files else None
