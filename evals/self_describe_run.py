"""CLI: live SELF_DESCRIBE trigger-accuracy probe against the real app (see
self_describe_harness.py for the pure scoring logic this drives). For each
dataset item, asks `question` (optionally preceded by one `prior_exchange`
turn, for the meta-question-about-a-prior-answer traps) through the real
orchestrator with SELF_DESCRIBE=true, and checks whether the
app_capabilities path fired — the appended note always starts with
"Verified capabilities" (see self_describe.format_note), a marker that
appears if and only if the tool was actually called or the phrase heuristic
fired for a LiteLLM-routed model.

Makes real API calls (OPENAI_API_KEY, and OPENAI_MODEL_SMART/whatever the
smart tier resolves to must be a model this app offers the app_capabilities
tool to for a meaningful test of the TOOL path — see
orchestrator._SELF_DESCRIBE_TOOL_PROVIDERS).

Deliberately excluded from CI (see .github/workflows/ci.yml's `pytest
tests -q` — this lives in evals/, not tests/, and needs a real key and
real network access unlike everything CI runs).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .self_describe_harness import evaluate, load_dataset, summarize


def _make_prober():
    """Deferred imports: this module (and everything it imports) must not
    be imported before DATABASE_PATH/SELF_DESCRIBE are set in main() below —
    app.database reads DATABASE_PATH at import time.
    """
    from app.orchestrator import run_orchestrator
    from app.schemas import AskRequest, Mode

    owner = "self-describe-eval"

    def probe(item: dict) -> bool:
        history = ""
        prior = item.get("prior_exchange")
        if prior:
            history = f"user: {prior['question']}\nassistant: {prior['answer']}\n\n"

        result = run_orchestrator(
            AskRequest(question=item["question"], mode=Mode.auto),
            owner=owner,
            history=history,
        )
        return "Verified capabilities" in result.answer

    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe SELF_DESCRIBE trigger accuracy: for each dataset "
        "item, checks whether the app_capabilities path fires when it "
        "should (a direct question about the app) and stays silent when it "
        "shouldn't (a meta-question about a prior answer, a general AI "
        "question, incidental phrasing). Makes real API calls, so "
        "OPENAI_API_KEY must be set."
    )
    parser.add_argument("--dataset", help="Path to a dataset JSON file.", default=None)
    parser.add_argument(
        "--max-false-positive-rate",
        type=float,
        default=0.0,
        help=(
            "Exit non-zero if the false-positive rate (misfired on a "
            "should_fire: false trap) exceeds this (0..1). Default 0.0 -- "
            "ANY misfire fails the run by default, since this is exactly "
            "the 'wrong tool firing again' complaint being tracked."
        ),
    )
    parser.add_argument(
        "--max-false-negative-rate",
        type=float,
        default=0.34,
        help=(
            "Exit non-zero if the false-negative rate (missed a genuine "
            "should_fire: true question) exceeds this (0..1). Looser than "
            "the false-positive gate by design -- missing a capabilities "
            "question is an annoyance, not the misfire this eval exists "
            "to catch, and the phrase-heuristic fallback path (non-tool-"
            "calling providers) is inherently less precise than the tool "
            "path."
        ),
    )
    args = parser.parse_args(argv)

    scratch_dir = tempfile.mkdtemp(prefix="ai-orchestrator-self-describe-eval-")
    os.environ["DATABASE_PATH"] = str(Path(scratch_dir) / "eval.db")
    os.environ["SELF_DESCRIBE"] = "true"
    os.environ.setdefault("RESPONSE_CACHE", "false")

    from app.database import init_db

    init_db()

    dataset = load_dataset(args.dataset)
    results = evaluate(dataset, _make_prober())
    summary = summarize(results)

    print(
        f"Accuracy: {summary['correct']}/{summary['total']} = {summary['accuracy']:.1%}"
    )
    print(
        f"False positives (misfired on a trap): "
        f"{len(summary['false_positive_ids'])}/{summary['should_not_fire_total']} = "
        f"{summary['false_positive_rate']:.1%}"
    )
    print(
        f"False negatives (missed a genuine question): "
        f"{len(summary['false_negative_ids'])}/{summary['should_fire_total']} = "
        f"{summary['false_negative_rate']:.1%}\n"
    )

    if summary["false_positive_ids"]:
        print("Misfired on (should NOT have called app_capabilities):")
        for item_id in summary["false_positive_ids"]:
            print(f"  - {item_id}")
    if summary["false_negative_ids"]:
        print("Missed (SHOULD have called app_capabilities):")
        for item_id in summary["false_negative_ids"]:
            print(f"  - {item_id}")

    failed = False
    if summary["false_positive_rate"] > args.max_false_positive_rate:
        print(
            f"\nFAIL: false-positive rate {summary['false_positive_rate']:.1%} "
            f"exceeds --max-false-positive-rate {args.max_false_positive_rate:.1%}"
        )
        failed = True
    if summary["false_negative_rate"] > args.max_false_negative_rate:
        print(
            f"\nFAIL: false-negative rate {summary['false_negative_rate']:.1%} "
            f"exceeds --max-false-negative-rate {args.max_false_negative_rate:.1%}"
        )
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
