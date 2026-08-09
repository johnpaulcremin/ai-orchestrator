"""Re-run attribution: the write half of making a retry visible as a COST.

The router optimises for predicted cost, and every ledger in this app agreed
with it, because every ledger only ever saw the first attempt. A cheap answer
regenerated twice costs more than a dearer one that lands first time, and
nothing could show that — not because the join was missing, but because a
retry DESTROYS the evidence:

  - regenerate deletes the answer it replaces (delete_messages_after) and
    inserts a fresh one, taking that attempt's mode_used/model/cost_usd/
    feedback with it. messages.notes gains "regenerated", which says the
    answer you are looking at is a retry — never what it replaced, and never
    how many times, since notes is rebuilt from each fresh orchestrator
    result rather than accumulated.
  - edit deletes the user turn as well (delete_messages_from) and re-inserts
    it under a new id, so the TURN's identity goes too.
  - spend_log does keep the money (it is written per billable call,
    independent of message persistence) but carries no conversation,
    message, category or tier column to hang it on.

So this module snapshots the attempt about to be replaced BEFORE the delete,
and appends both it and its replacement to retry_log (see app/database.py's
CREATE TABLE comment for the column semantics). app/retry_cost.py reads that
back. Nothing here is derivable after the fact, which is the only reason a
new ledger exists rather than an analysis over the five that already do.

MEASUREMENT ONLY, on the same terms as app/correction_tracking.py: no
function here has a return value a caller acts on, nothing it writes reaches
a model or a routing decision, and no retry is triggered, suppressed or
re-ordered because of it. The escalation cascade this data is eventually for
stays on the backlog until there is enough of it to justify a threshold.

WHY THE SIGNAL IS NOT ONE COUNTER. A regeneration is ambiguous. "Show me
another angle" and "that answer was wrong" are the same button, and a naive
retry count reads both as failure — which would push routing to spend more on
categories where the user was merely browsing. So the reason is recorded as
one of SIGNALS, kept distinct all the way through to the report:

  regenerated_unrated         the replaced answer carried no rating. May be
                              taste; the honest default, and deliberately not
                              folded in with the 👎 case.
  regenerated_after_downvote  the replaced answer was rated 👎. The one signal
                              that is unambiguously a quality failure.
  regenerated_after_upvote    the replaced answer was rated 👍. Its own bucket
                              rather than "unrated", which would quietly
                              report a rated answer as unrated.
  edited                      the user rewrote their own prompt and re-asked
                              (the edit path). A retry the user paid for with
                              their own work, which says as much about the
                              first answer as about the question.

NO FEATURE FLAG, unlike CORRECTION_TRACKING's otherwise-identical "local,
passive, zero-cost" profile. Three reasons: it records nothing the messages
table did not already hold about the caller's own answers (no message text,
no question text); it is the substrate the report is computed FROM rather
than an optional extra signal beside it; and a half-populated ledger is worse
than an absent one, because first-attempt cost would silently undercount for
the window the flag was off in, with nothing in the numbers to show it.

KNOWN LIMITS, stated here because they belong in the numbers' caveats too:
  - Retries that happened before this ledger existed are invisible; the first
    retry of an older turn records the replaced answer as attempt 1.
  - A FAILED retry (empty answer) replaces nothing, so it is recorded as
    nothing — but it can still have cost money. That spend is in spend_log
    and stays unattributable, the same way it always was.
"""

from __future__ import annotations

from typing import Any

from . import database
from .feedback import lane_from_mode_used, parse_mode_used
from .telemetry import logger

SIGNAL_REGENERATED_UNRATED = "regenerated_unrated"
SIGNAL_REGENERATED_AFTER_DOWNVOTE = "regenerated_after_downvote"
SIGNAL_REGENERATED_AFTER_UPVOTE = "regenerated_after_upvote"
SIGNAL_EDITED = "edited"

SIGNALS: tuple[str, ...] = (
    SIGNAL_REGENERATED_UNRATED,
    SIGNAL_REGENERATED_AFTER_DOWNVOTE,
    SIGNAL_REGENERATED_AFTER_UPVOTE,
    SIGNAL_EDITED,
)

# Read by self_report.py's report section and the Settings panel. Each label
# names what the signal can support, not just what happened, since the whole
# point of keeping them apart is that they mean different things.
SIGNAL_LABELS: dict[str, str] = {
    SIGNAL_REGENERATED_UNRATED: "Regenerated, unrated (may be taste)",
    SIGNAL_REGENERATED_AFTER_DOWNVOTE: "Regenerated after 👎 (quality failure)",
    SIGNAL_REGENERATED_AFTER_UPVOTE: "Regenerated after 👍 (not a failure)",
    SIGNAL_EDITED: "Edited and re-asked (user did the work)",
}


def classify_signal(kind: str, replaced_feedback: int | None) -> str:
    """Which of SIGNALS this retry is. `kind` is "edit" or "regenerate" (the
    route family that asked for it); `replaced_feedback` is the messages.
    feedback value of the answer being replaced — 1, -1, or None for never
    rated / rated then cleared, which read the same here and are both
    "unrated" (see that column's migration comment).

    The edit path wins over any rating: a user who rewrites their own prompt
    has told us more about the turn than their click did.
    """
    if kind == "edit":
        return SIGNAL_EDITED
    if replaced_feedback == -1:
        return SIGNAL_REGENERATED_AFTER_DOWNVOTE
    if replaced_feedback == 1:
        return SIGNAL_REGENERATED_AFTER_UPVOTE
    return SIGNAL_REGENERATED_UNRATED


def snapshot_turn(conversation_id: int, user_message_id: int) -> dict[str, Any] | None:
    """Everything record_retry will need about the attempt being replaced,
    read while it still exists. Call this BEFORE the delete; call record_retry
    after the replacement has been persisted.

    Returns None when there is nothing to attribute — no assistant answer
    after `user_message_id`, i.e. nothing is being replaced and no earlier
    attempt's cost is at risk of being lost. A regenerate on a turn whose
    previous attempt failed lands here, correctly recording no retry.

    Best-effort by contract: a read failure returns None (the retry then goes
    unmeasured) rather than propagating into an answer that has already been
    paid for.
    """
    try:
        replaced_rows = database.replaced_answer_rows(conversation_id, user_message_id)
        if not replaced_rows:
            return None

        resolved = database.retry_turn_key(conversation_id, user_message_id)
        turn_key = user_message_id if resolved is None else resolved
        chain = [] if resolved is None else database.retry_log_chain(turn_key)

        first = replaced_rows[0]
        # The routing decision of the FIRST answer to this turn, but the cost
        # of every row being deleted — normally the same single answer (see
        # replaced_answer_rows), summed rather than assumed so no replaced
        # spend is dropped if there is more than one. Stays None when NONE of them
        # was priced, rather than summing to a $0 that would read as free —
        # the same NULL-means-unpriced convention as spend_log.cost_usd.
        priced = [
            float(row["cost_usd"])
            for row in replaced_rows
            if row["cost_usd"] is not None
        ]
        cost = sum(priced) if priced else None
        return {
            "turn_key": turn_key,
            "user_message_id": user_message_id,
            "next_index": max((int(r["attempt_index"]) for r in chain), default=0) + 1,
            "recorded_message_ids": {
                int(r["message_id"]) for r in chain if r["message_id"] is not None
            },
            "replaced": {
                "message_id": int(first["id"]),
                "mode_used": first["mode_used"],
                "model": first["model"],
                "cost_usd": cost,
                "feedback": first["feedback"],
                "created_at": first["created_at"],
            },
        }
    except Exception:  # pragma: no cover - defense in depth, see logger.exception
        logger.exception(
            "retry_attribution.snapshot_failed conversation_id=%s", conversation_id
        )
        return None


def record_retry(
    owner: str | None,
    conversation_id: int,
    snapshot: dict[str, Any] | None,
    *,
    kind: str,
    new_message_id: int | None,
    new_user_message_id: int | None = None,
    mode_used: str | None,
    model: str | None,
    cost_usd: float | None,
) -> None:
    """Append this retry to retry_log: the attempt it replaced (unless that
    one is already recorded, i.e. this is the turn's second or later retry),
    then the replacement itself.

    `snapshot` is snapshot_turn's return value, taken before the delete —
    None means there was nothing to attribute and this is a no-op.
    `new_user_message_id` is the id the user turn has AFTER the retry, which
    only differs from the snapshot's on the edit path (where the user row was
    deleted and re-inserted); pass None to keep the snapshot's.

    Best-effort by contract, same as snapshot_turn: this runs after the
    answer is already persisted and served, so a write failure is logged and
    swallowed. Losing a measurement row must never turn a delivered answer
    into a 500, or break a stream mid-flight.
    """
    if snapshot is None:
        return
    try:
        replaced = snapshot["replaced"]
        turn_key = int(snapshot["turn_key"])
        index = int(snapshot["next_index"])

        if int(replaced["message_id"]) not in snapshot["recorded_message_ids"]:
            _record(
                owner,
                conversation_id,
                turn_key=turn_key,
                user_message_id=int(snapshot["user_message_id"]),
                message_id=int(replaced["message_id"]),
                attempt_index=index,
                # No signal on the attempt being replaced: it is an ORIGINAL
                # answer (or an earlier retry recorded at its own turn), and
                # the signal describes why a retry happened, not what was
                # retried.
                signal=None,
                mode_used=replaced["mode_used"],
                model=replaced["model"],
                cost_usd=replaced["cost_usd"],
                created_at=replaced["created_at"],
            )
            index += 1

        _record(
            owner,
            conversation_id,
            turn_key=turn_key,
            user_message_id=(
                int(new_user_message_id)
                if new_user_message_id is not None
                else int(snapshot["user_message_id"])
            ),
            message_id=int(new_message_id) if new_message_id is not None else None,
            attempt_index=index,
            signal=classify_signal(kind, replaced["feedback"]),
            mode_used=mode_used,
            model=model,
            cost_usd=cost_usd,
            created_at=None,
        )
    except Exception:  # pragma: no cover - defense in depth, see logger.exception
        logger.exception(
            "retry_attribution.record_failed conversation_id=%s", conversation_id
        )


def _record(
    owner: str | None,
    conversation_id: int,
    *,
    turn_key: int,
    user_message_id: int | None,
    message_id: int | None,
    attempt_index: int,
    signal: str | None,
    mode_used: str | None,
    model: str | None,
    cost_usd: float | None,
    created_at: str | None,
) -> None:
    """One retry_log row, with category/tier/model derived from mode_used the
    same way feedback_log and correction_log derive theirs — same parser, so a
    category or lane means exactly the same thing in all three ledgers."""
    database.record_retry_attempt(
        owner=owner,
        conversation_id=conversation_id,
        turn_key=turn_key,
        user_message_id=user_message_id,
        message_id=message_id,
        attempt_index=attempt_index,
        signal=signal,
        mode_used=mode_used,
        model=model or parse_mode_used(mode_used)[0],
        category=parse_mode_used(mode_used)[1],
        tier=lane_from_mode_used(mode_used),
        cost_usd=cost_usd,
        created_at=created_at,
    )
