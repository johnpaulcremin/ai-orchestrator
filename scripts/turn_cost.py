#!/usr/bin/env python3
"""What one conversation's turns actually cost, including retries.

    python scripts/turn_cost.py --conversation 42

Written for one specific job: reading the evidence out of a REAL run after the
fact, when the question is "how much did this turn really cost, and how many
attempts did it take" — the truncation/Continue case in particular, where the
answer is not visible in the UI and only partly visible in the ledgers.

READ-ONLY. The database is opened with `mode=ro` and nothing here writes, so it
is safe to point at a live database while the app is running.

WHAT IT CAN AND CANNOT SHOW, stated here because the gaps are the point:

  * Retries (regenerate / edit) are exact. retry_log records one row per
    attempt with that attempt's own routing decision and cost, so
    first-attempt cost, true cost and the multiplier are all real numbers —
    see app/retry_cost.py.
  * CONTINUATIONS ARE NOT COUNTED ANYWHERE. `database.append_to_message` folds
    a continuation's tokens and cost into the SAME message row and keeps no
    counter, so a turn continued five times is indistinguishable from one that
    answered in a single call, except that its cost is larger. The number of
    Continue clicks has to be counted by hand while clicking. This is a real
    gap in the schema, not a limitation of this script.
  * The `calls in the window` figure below is the closest available proxy for
    that count: spend_log has one row per billable model call, but no
    conversation_id, so it can only be windowed by TIME. It is only meaningful
    if the run was the only thing happening. Router/classifier calls do NOT
    appear there (app/routing.py records no spend), but library-upload
    embeddings and transcription/speech DO, so a run that uploaded a document
    will over-count.
  * No rates are printed. One conversation is n=1, and a percentage from a
    single sample is exactly the thing app/retry_cost.py exists to avoid
    printing. Counts and costs only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.database import _db_path  # noqa: E402
from app.feedback import lane_from_mode_used, parse_mode_used  # noqa: E402


def _connect(db: Path) -> sqlite3.Connection:
    """Read-only, so this can be pointed at a live database safely."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _usd(value: float | None) -> str:
    if value is None:
        return "unpriced"
    return f"${value:.4f}"


def _route(mode_used: str | None) -> str:
    """ "fast:coding" — the tier and category as the ledgers parse them, so this
    script and app/retry_cost.py never disagree about what a lane is."""
    if not mode_used:
        return "—"
    tier = lane_from_mode_used(mode_used) or "?"
    category = parse_mode_used(mode_used)[1]
    return f"{tier}:{category}" if category else tier


def _turns(conn: sqlite3.Connection, conversation_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, role, content, mode_used, model, truncated,
               input_tokens, output_tokens, cost_usd, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id
        """,
        (conversation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _retry_chains(
    conn: sqlite3.Connection, conversation_id: int
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT turn_key, message_id, attempt_index, signal, mode_used, model,
               category, tier, cost_usd, created_at
        FROM retry_log
        WHERE conversation_id = ?
        ORDER BY turn_key, attempt_index, id
        """,
        (conversation_id,),
    ).fetchall()
    chains: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        chains.setdefault(int(row["turn_key"]), []).append(dict(row))
    return chains


def _calls_in_window(conn: sqlite3.Connection, minutes: int) -> tuple[int, float]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS spend
        FROM spend_log
        WHERE created_at >= datetime('now', ?)
        """,
        (f"-{minutes} minutes",),
    ).fetchone()
    return int(row["calls"]), float(row["spend"])


def _print_turns(turns: list[dict[str, Any]]) -> None:
    print("TURNS")
    print(
        f"  {'id':>5}  {'role':<9} {'route':<22} {'cut off':<8} "
        f"{'in':>7} {'out':>7} {'cost':>10}  first words"
    )
    for turn in turns:
        content = " ".join(str(turn["content"] or "").split())[:44]
        cut_off = "yes" if turn["truncated"] else ""
        tokens_in = turn["input_tokens"]
        tokens_out = turn["output_tokens"]
        print(
            f"  {turn['id']:>5}  {turn['role']:<9} {_route(turn['mode_used']):<22} "
            f"{cut_off:<8} {tokens_in if tokens_in is not None else '—':>7} "
            f"{tokens_out if tokens_out is not None else '—':>7} "
            f"{_usd(turn['cost_usd']):>10}  {content}"
        )
    answered = [t for t in turns if t["role"] == "assistant"]
    total = sum(float(t["cost_usd"] or 0.0) for t in answered)
    print()
    print(
        f"  {len(answered)} assistant message(s), {_usd(total)} on the rows themselves"
    )
    print("  NB: an assistant row's cost already INCLUDES every continuation folded")
    print("      into it, and no column says how many there were.")


def _print_retries(chains: dict[int, list[dict[str, Any]]]) -> None:
    print()
    print("RE-RUNS (regenerate / edit — exact, from retry_log)")
    if not chains:
        print("  none: no turn in this conversation was regenerated or edited.")
        print("  A Continue is NOT a retry and never appears here — it extends the")
        print("  existing answer rather than replacing it.")
        return
    for turn_key, attempts in sorted(chains.items()):
        first = float(attempts[0]["cost_usd"] or 0.0)
        total = sum(float(a["cost_usd"] or 0.0) for a in attempts)
        multiplier = f"{total / first:.2f}x" if first > 0 else "—"
        print(
            f"  turn {turn_key}: {len(attempts)} attempt(s), "
            f"first {_usd(first)} -> true {_usd(total)} ({multiplier})"
        )
        for attempt in attempts:
            signal = attempt["signal"] or "(original)"
            route = _route(attempt["mode_used"])
            print(
                f"      #{attempt['attempt_index']} {route:<20} "
                f"{_usd(attempt['cost_usd']):>10}  {signal}"
            )
        print(
            "      attributed to the ORIGINAL decision "
            f"({_route(attempts[0]['mode_used'])}) — see app/retry_cost.py"
        )


def _print_calls(calls: int, spend: float, minutes: int) -> None:
    print()
    print(f"MODEL CALLS in the last {minutes} minutes (spend_log, WHOLE DATABASE)")
    print(f"  {calls} call(s), {_usd(spend)}")
    print("  The only available proxy for how many Continue clicks a turn took:")
    print("  spend_log has no conversation_id, so this is time-windowed, not")
    print("  conversation-scoped. Trustworthy only if the run was the only")
    print("  activity. Router/classifier calls are absent (they record no spend);")
    print("  library-upload embeddings and transcription/speech are present.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--conversation", type=int, required=True, help="conversation id to report on"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="database file (defaults to the app's own DATABASE_PATH resolution)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=30,
        help="window for the spend_log call count (default 30)",
    )
    args = parser.parse_args(argv)

    db = args.db or _db_path()
    if not Path(db).exists():
        print(f"No database at {db}", file=sys.stderr)
        return 2

    print(f"database:     {db}")
    print(f"conversation: {args.conversation}")
    print()

    with _connect(Path(db)) as conn:
        turns = _turns(conn, args.conversation)
        if not turns:
            print(f"No messages in conversation {args.conversation}.", file=sys.stderr)
            return 1
        _print_turns(turns)
        _print_retries(_retry_chains(conn, args.conversation))
        calls, spend = _calls_in_window(conn, args.minutes)
        _print_calls(calls, spend, args.minutes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
