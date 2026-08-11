"""CLI: golden answer-quality run against the real orchestrator (see
golden_harness.py for the pure scoring/drift logic this drives). Each
dataset prompt is asked through the full auto-routing pipeline, its answer
judged against deterministic checks, and the run persisted to
evals/results/golden-<timestamp>.json — the NEXT run then reports drift
against it: items that regressed, items that recovered, and items a
different model answered (quiet provider drift, visible before quality
moves).

Makes real API calls at your configured models' real prices (the whole
point — it measures the live deployment). The default dataset is ~14
prompts across all 11 task categories; expect roughly the cost of 14
ordinary questions. `--limit N` runs a cheaper smoke.

Both response caches are forced off for the run: a cached answer would
measure the cache, not the model, and corrupt the drift signal.

By default the run uses a scratch database, so routing resolves from env
vars only — a saved Settings override (which lives in the DB) is invisible.
Pass --database path/to/ai_orchestrator.db to evaluate the routing your real
deployment actually does; the eval only reads settings and writes nothing
but its own spend rows there, but point it at a copy if that matters.

Deliberately excluded from CI, same as every evals/ runner: needs real keys
and real network, and costs real money.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .golden_harness import (
    RESULTS_DIR,
    compare_runs,
    latest_results_file,
    load_dataset,
    score_item,
    summarize,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the golden answer-quality prompts through the real "
        "orchestrator and report pass rate plus drift against the previous "
        "persisted run. Makes real, paid API calls."
    )
    parser.add_argument("--dataset", default=None, help="Path to a dataset JSON file.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Run only the first N items (smoke)."
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Use this SQLite file instead of a scratch one, so saved Settings "
        "overrides participate in routing. Read-mostly; the run's own spend "
        "rows are written to it.",
    )
    parser.add_argument(
        "--no-save", action="store_true", help="Don't persist this run's results."
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero if any item passed in the previous run and failed "
        "in this one.",
    )
    args = parser.parse_args(argv)

    if args.database:
        os.environ["DATABASE_PATH"] = args.database
    else:
        scratch = tempfile.mkdtemp(prefix="ai-orchestrator-golden-eval-")
        os.environ["DATABASE_PATH"] = str(Path(scratch) / "eval.db")
    # A cached answer measures the cache, not the model.
    os.environ["RESPONSE_CACHE"] = "false"
    os.environ["SEMANTIC_CACHE"] = "false"

    from app.database import init_db
    from app.orchestrator import run_orchestrator
    from app.schemas import AskRequest, Mode

    init_db()

    items = load_dataset(Path(args.dataset) if args.dataset else None)
    if args.limit is not None:
        items = items[: args.limit]

    results = []
    for item in items:
        response = run_orchestrator(
            AskRequest(question=item["prompt"], mode=Mode.auto), owner="golden-eval"
        )
        row = score_item(item, response.answer, response.mode_used, response.cost_usd)
        marker = "PASS" if row["passed"] else "FAIL"
        print(f"  [{marker}] {item['id']}  ({row['mode_used']})")
        if not row["passed"]:
            for check in row["failed_checks"]:
                print(f"         failed check: {check}")
        results.append(row)

    summary = summarize(results)
    rate = f"{summary['pass_rate']:.1%}" if summary["pass_rate"] is not None else "n/a"
    print(f"\nPass rate: {summary['passed']}/{summary['total']} = {rate}")
    print(f"Run cost:  ${summary['total_cost_usd']:.4f}")
    for category, slot in sorted(summary["by_category"].items()):
        print(f"  {category}: {slot['passed']}/{slot['total']}")

    regressed = False
    previous_file = latest_results_file()
    if previous_file is not None:
        previous = json.loads(previous_file.read_text(encoding="utf-8"))["results"]
        drift = compare_runs(previous, results)
        print(f"\nDrift vs {previous_file.name}:")
        if not any(drift.values()):
            print("  none — identical outcomes, same models.")
        for item_id in drift["regressions"]:
            print(f"  REGRESSED: {item_id}")
        for item_id in drift["fixes"]:
            print(f"  recovered: {item_id}")
        for change in drift["model_changes"]:
            print(
                f"  model changed: {change['id']}  {change['from']} -> {change['to']}"
            )
        for item_id in drift["only_in_previous"]:
            print(f"  removed from dataset since: {item_id}")
        for item_id in drift["only_in_current"]:
            print(f"  new in dataset: {item_id}")
        regressed = bool(drift["regressions"])
    else:
        print("\nNo previous run to compare against — this run is the baseline.")

    if not args.no_save:
        RESULTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = RESULTS_DIR / f"golden-{stamp}.json"
        out.write_text(
            json.dumps(
                {"generated_at": stamp, "results": results, "summary": summary},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nSaved: {out}")

    return 1 if (args.fail_on_regression and regressed) else 0


if __name__ == "__main__":
    sys.exit(main())
