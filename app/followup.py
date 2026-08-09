"""Follow-up routing: how a turn whose meaning depends on the PREVIOUS
assistant turn gets routed.

Some user turns are not self-contained requests. Their meaning is relative to
what the assistant just said, and routing them as standalone requests — which
is what the auto classifier does to everything it is handed — breaks them in a
way that is specific to each case but identical in cause. Two live instances,
both of which this module now owns:

  CONTINUE. "Emit the rest of that text." Dispatched as Mode.auto it could come
  back as a clarifying question (a continuation is a purely referential
  request, so the ambiguity rule fires on it), as a fresh multi-step workflow
  replanned off the truncated answer's own text, or at the fast tier's cap —
  a third of the cap that had just proven too small.

  A CLARIFY ANSWER. "Both." Dispatched as Mode.auto it comes back as another
  clarifying question, because a bare reply naming no subject is maximally
  ambiguous and the two candidate readings are sitting in the history the
  ambiguity rule consults — the assistant put them there itself. Observed
  live: three clarifies in a row, each costing a router call and answering
  nothing, before the fourth turn finally answered.

The cure is the same in both cases and is the whole content of this module:
read the previous assistant turn's `mode_used`, and derive the follow-up's
routing from it instead of classifying the follow-up afresh. `mode_used` is
already persisted on every answer and already parses into a lane via
app/feedback.py's `lane_from_mode_used`, so the signal needed has existed all
along — nothing read it.

Keyed on the LANE, deliberately, rather than on which endpoint is calling:
"clarify" and a truncated answer are two values of one dimension, and a third
case (whatever it turns out to be) belongs here as a third lane rather than as
a third bespoke path in a route module. That is the difference between removing
this mechanism and patching its instances one at a time.

WHAT EACH CASE NEEDS, since they are not identical:
  - Continue needs only the previous DECISION (resume at that tier) — see
    resume_route, moved here from routers/messages/ask.py.
  - A clarify answer additionally needs the previous CONTENT: the original
    request has to be recombined with the reply, because routing on the reply
    alone is precisely what fails. See clarify_followup.

MEASUREMENT-FREE. Nothing here writes a ledger row or reads a setting; it is
pure routing derivation, and every function is a pure function of the messages
it is handed.
"""

from __future__ import annotations

from typing import Any

from .feedback import lane_from_mode_used, parse_mode_used
from .schemas import Mode

# The lane a clarifying question is persisted under — decide_route sets
# mode_used="auto->clarify" for it, so lane_from_mode_used returns this.
CLARIFY_LANE = "clarify"

# Prepended to the recombined request when a clarify answer is being routed and
# a further clarify has been forbidden. The model has both the question it asked
# and the reply in its own conversation context, so it can name the reading it
# picked; this only tells it that it must pick one and say which.
ASSUMPTION_INSTRUCTION = (
    "The user's latest message answers the clarifying question you asked just "
    "before it. Treat the two together as one request and answer it now. Do "
    "not ask for clarification again. If more than one reading still seems "
    "possible, choose the most likely one, say which reading you assumed in a "
    "single short line first, and then answer."
)


def resume_route(mode_used: str | None, model: str | None) -> tuple[Mode, str | None]:
    """The (mode, forced model) a CONTINUATION must run under: the routing
    decision that produced the answer being continued, never Mode.auto.

    Moved here from routers/messages/ask.py unchanged in behaviour — see this
    module's docstring for why it belongs beside the clarify case rather than
    in the route module that happens to call it.

    Routing at an explicit tier is what removes the three failure modes at
    once, because `decide_route` short-circuits on an explicit mode BEFORE it
    classifies anything: no classifier call, so no ambiguity verdict, no
    `multi_part`, and the cap is the one the original answer had.

      smart/fast/budget -> that Mode, hence that tier's own cap again.
      forced:<model>    -> Mode.auto plus that model, which is how a forced
                           answer was produced in the first place (forced
                           model, smart-tier cap — see routing.decide_route).
      auto->free:<model>-> the same: continue on the model that answered. It
                           re-dispatches as a forced model rather than through
                           the free lane (whose eligibility rules exclude a
                           forced model), which costs nothing extra because
                           that model is priced at $0 anyway.
      anything else     -> Mode.smart. Covers a legacy row with no mode_used,
                           and "workflow"/"self_report"/"clarify", none of
                           which name a single-shot tier. Smart, not auto: the
                           point is to not re-classify, and smart is the most
                           generous single-shot cap available.

    Deliberately NOT a cap increase. Continuing at the original's own tier is
    what "resume" means, and each continuation gets a fresh full cap of that
    size, so total output grows without bound across clicks. Whether the remedy
    should ALSO escalate the ceiling is a separate decision with its own cost
    implications, and is not smuggled in here.
    """
    lane = lane_from_mode_used(mode_used)
    if lane == "smart":
        return Mode.smart, None
    if lane == "fast":
        return Mode.fast, None
    if lane == "budget":
        return Mode.budget, None
    if lane in ("forced", "free"):
        return Mode.auto, model or parse_mode_used(mode_used)[0]
    return Mode.smart, None


def last_assistant_was_clarify(prior_messages: list[dict[str, Any]]) -> bool:
    """Whether the message immediately before this new user turn is a
    clarifying question this app asked.

    `prior_messages` is the conversation as it stood BEFORE the new turn was
    persisted — exactly what the ask paths already fetch and already hand to
    build_recent_history_snippet, so this costs no extra query.

    Only the IMMEDIATELY preceding message counts. A clarify three turns back
    that was already answered is finished business, and treating a later
    unrelated turn as its answer would recombine two unrelated requests.
    """
    if not prior_messages:
        return False
    previous = prior_messages[-1]
    if previous.get("role") != "assistant":
        return False
    return lane_from_mode_used(previous.get("mode_used")) == CLARIFY_LANE


def clarify_followup(prior_messages: list[dict[str, Any]], reply: str) -> str | None:
    """The routing question for a reply to a clarifying question: the ORIGINAL
    request recombined with the reply, or None when this is not a clarify
    answer at all (in which case the caller routes normally).

    Routing on the reply in isolation is the bug. "Both" carries no category,
    no complexity and no subject; it is maximally ambiguous and always will be,
    so no amount of classifier tuning fixes it. The original request is where
    the routable content lives — in the observed loop, "what are your
    strengths" is a `planning`-ish question and "both" is nothing at all.

    The original request is the last USER turn before the clarify. A clarify
    consumes no user turn of its own, so that is simply the user message
    preceding it; the search walks back rather than assuming a fixed offset,
    since an aborted/cancelled assistant message can sit in between.

    Returns just the combined text. The caller pairs it with
    `allow_clarify=False` (see routing.decide_route) — recombining alone is not
    enough of a guarantee, because a recombined request can still read as
    ambiguous, and the whole point is that a second clarify in a row must be
    impossible rather than unlikely.
    """
    if not last_assistant_was_clarify(prior_messages):
        return None

    clarify_index = len(prior_messages) - 1
    original: str | None = None
    for message in reversed(prior_messages[:clarify_index]):
        if message.get("role") == "user":
            original = str(message.get("content", "")).strip()
            break
    if not original:
        # A clarify with no user turn behind it should not be reachable (the
        # classifier needs history to find an ambiguous reference at all), but
        # if it happens there is nothing to recombine — route on the reply and
        # rely on the caller's allow_clarify=False to stop the loop anyway.
        return None

    clarifying_question = str(prior_messages[clarify_index].get("content", "")).strip()
    return (
        f"{original}\n\n"
        f"(You asked: {clarifying_question})\n"
        f"(The user answered: {reply.strip()})"
    )
