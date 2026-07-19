from __future__ import annotations

import argparse
import sys

from app.orchestrator import get_client
from app.routing import decide_route
from app.schemas import Mode

from .harness import evaluate, load_dataset, summarize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure auto-routing classifier accuracy against a labeled dataset. "
        "Makes real router (OPENAI_MODEL_ROUTER) calls, so OPENAI_API_KEY must be set."
    )
    parser.add_argument("--dataset", help="Path to a dataset JSON file.", default=None)
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.0,
        help="Exit non-zero if accuracy is below this (0..1). Default 0 (never fails).",
    )
    args = parser.parse_args(argv)

    client = get_client()

    def decide(prompt: str):
        return decide_route(prompt, Mode.auto, client=client)

    dataset = load_dataset(args.dataset)
    results = evaluate(dataset, decide)
    summary = summarize(results)

    print(
        f"Routing accuracy: {summary['correct']}/{summary['total']} "
        f"= {summary['accuracy']:.1%}\n"
    )
    print("Confusion (expected->predicted):")
    for key, count in summary["confusion"].items():
        print(f"  {key}: {count}")

    misroutes = [r for r in results if not r["correct"]]
    if misroutes:
        print("\nMisroutes:")
        for r in misroutes:
            print(
                f"  [{r['category']}] expected {r['expected']}, got {r['predicted']} "
                f"({r['model']}) :: {r['prompt'][:70]}"
            )

    if summary["accuracy"] < args.min_accuracy:
        print(
            f"\nFAIL: accuracy {summary['accuracy']:.1%} < min {args.min_accuracy:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
