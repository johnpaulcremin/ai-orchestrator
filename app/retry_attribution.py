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
  - A FAILED retry (empty answer) is recorded, with signal="failed" and no
    message id of its own (see record_failed_attempt) — it used to be recorded
    as nothing at all while still costing money. It counts in the turn's TOTAL
    cost but never in the retry rate: it replaced nothing, so treating it as a
    retry would inflate the rate with attempts that changed nothing.
  - A failure on a turn with NO answer yet is still invisible — there is no
    assistant row to anchor the attempt chain to. See record_failed_attempt's
    own docstring for why that is left alone rather than worked around.
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
SIGNAL_CONTINUED = "continued"
# An attempt that came back EMPTY. It replaced nothing (the previous answer is
# still there, because the delete is inside the "only on a real answer" guard),
# so it has no message_id of its own — but it consumed a full model call, and
# that money was previously recorded only in spend_log, where nothing can tie
# it back to the turn that caused it. Observed live: a 45-second, 5-step
# workflow attempt that produced no answer and left no attempt row.
SIGNAL_FAILED = "failed"

SIGNALS: tuple[str, ...] = (
    SIGNAL_REGENERATED_UNRATED,
    SIGNAL_REGENERATED_AFTER_DOWNVOTE,
    SIGNAL_REGENERATED_AFTER_UPVOTE,
    SIGNAL_EDITED,
    SIGNAL_CONTINUED,
    SIGNAL_FAILED,
)

# The retry signals proper — a further ATTEMPT at the same turn, replacing what
# was there. Two signals are deliberately NOT in here, for the same reason
# stated twice: each belongs in the COST of the turn but not in the retry rate.
#   SIGNAL_CONTINUED extends an answer rather than replacing it.
#   SIGNAL_FAILED replaced nothing — it produced no answer at all. Counting it
#     as a retry would inflate the rate with attempts that changed nothing, and
#     the denominator (turns) has no matching notion of a failed turn; a turn
#     whose retry failed still shows its original answer. Failures get their own
#     count and their own column instead, so "the user asked again" and "asking
#     again produced nothing" stay separable — the same distinction that keeps a
#     continuation out of the rate.
RETRY_SIGNALS: tuple[str, ...] = (
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
    SIGNAL_CONTINUED: "Continued a cut-off answer (the cap was too small)",
    SIGNAL_FAILED: "Attempt returned nothing (paid for, no answer)",
}


def classify_signal(kind: str, replaced_feedback: int | None) -> str:
    """Which of SIGNALS this attempt is. `kind` is "edit", "regenerate" or
    "continue" (the route family that asked for it); `replaced_feedback` is the
    messages.feedback value of the answer being replaced — 1, -1, or None for
    never rated / rated then cleared, which read the same here and are both
    "unrated" (see that column's migration comment).

    A continuation is classified on its kind alone, never on the rating: it is
    not a judgement about the answer at all, it is the answer being finished.
    So is a failure, and for a sharper reason: an empty answer is not a verdict
    on the previous one, it is the absence of a new one, so the rating it was
    asked under says nothing about it.
    The edit path likewise wins over any rating — a user who rewrites their own
    prompt has told us more about the turn than their click did.
    """
    if kind == "failed":
        return SIGNAL_FAILED
    if kind == "continue":
        return SIGNAL_CONTINUED
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


def snapshot_continuation(
    conversation_id: int, message_id: int
) -> dict[str, Any] | None:
    """snapshot_turn's twin for a CONTINUATION, where nothing is replaced.

    A continuation extends `message_id` in place (database.append_to_message
    folds its tokens and cost into that same row), so there is no delete to read
    ahead of and no new message id afterwards. What has to be read first is the
    row's cost AS IT STANDS: once the append lands, the original answer's own
    cost is gone — summed into a total with the continuation — and the turn's
    first-attempt cost is unrecoverable. That is the whole gap this closes.
    Before it, a turn continued five times reported a 1.00x multiplier, because
    first-attempt cost and true cost were literally the same number.

    The turn is identified by the USER message before `message_id`, not by
    `message_id` itself, so a continued turn and a later regenerate of the same
    turn share one chain (see retry_turn_key).

    Returns None when there is nothing to attribute — no such message, or no
    user turn ahead of it. Best-effort by contract, exactly like snapshot_turn:
    a read failure loses the measurement, never the answer.
    """
    try:
        messages = database.list_messages(conversation_id)
        target = next((m for m in messages if int(m["id"]) == message_id), None)
        if target is None:
            return None

        user_message_id: int | None = None
        for message in reversed([m for m in messages if int(m["id"]) < message_id]):
            if message.get("role") == "user":
                user_message_id = int(message["id"])
                break
        if user_message_id is None:
            return None

        resolved = database.retry_turn_key(conversation_id, user_message_id)
        turn_key = user_message_id if resolved is None else resolved
        chain = [] if resolved is None else database.retry_log_chain(turn_key)

        return {
            "turn_key": turn_key,
            "user_message_id": user_message_id,
            "next_index": max((int(r["attempt_index"]) for r in chain), default=0) + 1,
            "recorded_message_ids": {
                int(r["message_id"]) for r in chain if r["message_id"] is not None
            },
            # The attempt being EXTENDED. On the first continuation this is the
            # original answer and its pre-append cost, recorded retroactively as
            # attempt 1. On later continuations its id is already in
            # recorded_message_ids, so record_retry skips it and only appends
            # the new attempt — which is why the accumulated cost being read
            # here does not double-count.
            "replaced": {
                "message_id": int(target["id"]),
                "mode_used": target["mode_used"],
                "model": target["model"],
                "cost_usd": target["cost_usd"],
                "feedback": target["feedback"],
                "created_at": target["created_at"],
            },
        }
    except Exception:  # pragma: no cover - defense in depth, see logger.exception
        logger.exception(
            "retry_attribution.snapshot_continuation_failed conversation_id=%s",
            conversation_id,
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


def record_failed_attempt(
    owner: str | None,
    conversation_id: int,
    anchor_message_id: int,
    *,
    kind: str,
    mode_used: str | None,
    model: str | None,
    cost_usd: float | None,
) -> None:
    """Record an attempt that came back EMPTY, so its cost stops vanishing.

    `anchor_message_id` is the user message the attempt was answering — the
    same id the success path passes to snapshot_turn. Unlike that path, the
    snapshot is taken HERE rather than by the caller, because on failure
    nothing was deleted: the answer being retried is still in place (the delete
    lives inside the caller's "only on a real answer" guard), so there is no
    window to read it before. One helper rather than three copies of
    snapshot-then-record at the two non-streaming guards and their streaming
    twin.

    What this buys: the failed attempt's own cost, attributed to the turn, and
    — just as important — the ORIGINAL answer recorded as attempt 1 at the same
    moment, which is what makes the turn's first-attempt cost survive a later
    successful retry. Both were previously lost: the money reached spend_log,
    where nothing can tie it to the turn that spent it.

    RESIDUAL LIMIT, stated rather than papered over: a failure on a turn that
    has NO answer yet (a first ask that returned nothing, or a second
    consecutive failed retry) still records nothing. snapshot_turn returns None
    when there is no assistant row to anchor to, and inventing a turn_key
    without one would mean a second way of identifying a turn — the thing
    `turn_key` exists to prevent. Narrower than the gap this closes, and it
    leaves the money in spend_log exactly as before rather than misattributing
    it.

    Best-effort by contract, like everything else here.
    """
    snapshot = snapshot_turn(conversation_id, anchor_message_id)
    record_retry(
        owner,
        conversation_id,
        snapshot,
        kind=kind,
        # A failed attempt has no message of its own: nothing was persisted,
        # which is the whole reason it was invisible.
        new_message_id=None,
        mode_used=mode_used,
        model=model,
        cost_usd=cost_usd,
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
