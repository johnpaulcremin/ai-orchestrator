"""CLI: live multi-artefact classification probe against the real router
(see multipart_harness.py for the pure scoring logic this drives).

For each dataset prompt this makes ONE real classifier call — the same
`decide_route` call the app itself makes, not a second bespoke one — and
reads `decision.multi_part`. AUTO_WORKFLOW does not need to be enabled: the
flag governs whether the app ACTS on the verdict, while this eval measures
the verdict itself.

Cheap by construction: the classifier is the small OPENAI_MODEL_ROUTER
model, and no answering call is ever made.

Note on the prefilter: `_prefilter_tier` short-circuits obvious prompts
before the classifier runs, and such a decision carries multi_part=False by
default. That is the correct answer for every prefiltered prompt in this
dataset (they are all should_fire: false), and it is a real property of the
routing path rather than an artefact of the eval — a prompt the prefilter
catches can never be auto-routed to a workflow.

Makes real API calls (OPENAI_API_KEY). Deliberately excluded from CI, like
every other module in evals/.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .multipart_harness import evaluate, load_dataset, summarize


def _make_prober():
    """Deferred imports — app.database reads DATABASE_PATH at import time,
    so nothing here may be imported before main() sets it."""
    from app.orchestrator import get_client
    from app.routing import decide_route
    from app.schemas import Mode

    client = get_client()

    def probe(item: dict) -> bool:
        decision = decide_route(item["prompt"], Mode.auto, client=client)
        return bool(decision.multi_part)

    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe multi-artefact classification accuracy: for each dataset "
            "prompt, checks whether the router judges it multi-part when it "
            "should (several distinct artefacts) and holds fire when it "
            "shouldn't (one answer covering several topics or sections). "
            "Makes real API calls, so OPENAI_API_KEY must be set."
        )
    )
    parser.add_argument("--dataset", help="Path to a dataset JSON file.", default=None)
    parser.add_argument(
        "--max-false-positive-rate",
        type=float,
        default=0.0,
        help=(
            "Exit non-zero if the false-positive rate (fired on a "
            "single-answer prompt) exceeds this (0..1). Default 0.0 -- ANY "
            "misfire fails the run, because this is the expensive "
            "direction: an ordinary question routed into a workflow costs "
            "several model calls instead of one, for no benefit."
        ),
    )
    parser.add_argument(
        "--max-false-negative-rate",
        type=float,
        default=0.34,
        help=(
            "Exit non-zero if the false-negative rate (missed a genuinely "
            "multi-artefact prompt) exceeds this (0..1). Deliberately much "
            "looser than the false-positive gate: a miss just leaves the "
            "request on the single-shot path, which is exactly where it "
            "went before this feature existed."
        ),
    )
    args = parser.parse_args(argv)

    scratch_dir = tempfile.mkdtemp(prefix="ai-orchestrator-multipart-eval-")
    os.environ["DATABASE_PATH"] = str(Path(scratch_dir) / "eval.db")
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
        f"False positives (fired on a single-answer prompt): "
        f"{len(summary['false_positive_ids'])}/{summary['should_not_fire_total']} = "
        f"{summary['false_positive_rate']:.1%}"
    )
    print(
        f"False negatives (missed a multi-artefact prompt): "
        f"{len(summary['false_negative_ids'])}/{summary['should_fire_total']} = "
        f"{summary['false_negative_rate']:.1%}\n"
    )

    if summary["false_positive_ids"]:
        print("Misfired on (should have stayed single-shot):")
        for item_id in summary["false_positive_ids"]:
            print(f"  - {item_id}")
    if summary["false_negative_ids"]:
        print("Missed (SHOULD have been judged multi-part):")
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
