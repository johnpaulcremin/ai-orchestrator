"""Implicit correction tracking: a soft, MEASUREMENT-ONLY signal for "the
model got this wrong", distinct from app/feedback.py's explicit 👍/👎.

Rationale: an explicit rating requires effort and is sparsely used, but a
user's very next message often carries the correction signal for free —
"that's not what I asked", "you didn't answer that", "wrong tool". This
module watches for a CURATED, unambiguous set of multi-word phrases in a new
user turn that immediately follows an assistant answer, and if one matches,
appends a flag AGAINST THAT PREVIOUS ANSWER to correction_log (see
app/database.py's CREATE TABLE comment): message id, model, category,
mode/lane, timestamp. It never stores the message text itself.

Hard boundaries, by design:
  - MEASUREMENT ONLY. record_if_correction has no return value callers act
    on, changes no routing decision, re-runs nothing, and is never surfaced
    to the model. It is called, at most, once per new user turn, purely for
    its side effect of appending a row.
  - Kept strictly separate from feedback_log/feedback.py: no correction flag
    ever writes to messages.feedback or feedback_log, so it can never
    pollute the explicit-rating stats those already report. See
    self_report.py for how the two are reported side by side, not merged.
  - Gated by CORRECTION_TRACKING (default ON — unlike WEB_SEARCH/
    CODE_EXECUTION/etc., this spends no tokens, calls no model, and changes
    no answering behavior; it's as cheap and passive as a bookmark).

Phrase-list design, learning from the FACT_CHECK/SELF_DESCRIBE phrase-list
post-mortems (see self_describe.py's _SELF_DESCRIBE_PHRASES comment): every
phrase here is an unambiguous multi-word fragment, never a bare word like
"wrong" or "no" that would fire on an unrelated sentence ("the report's
number is wrong", "no worries") just because the word appears. Two more
precautions on top of the phrase list itself:
  - Quoted spans are stripped before matching, so a message that merely
    QUOTES a correction phrase (e.g. relaying someone else's complaint)
    doesn't get misread as the caller's own correction.
  - Only the first sentence is checked (correction phrasing overwhelmingly
    leads a message; a phrase appearing only deep in a longer message is far
    more likely to be about something else entirely, e.g. a document being
    discussed).
"""

from __future__ import annotations

import re
from typing import Any

from .feedback import lane_from_mode_used, parse_mode_used

# Deliberately narrow and high-precision: every phrase names the assistant's
# prior turn as its own subject ("that", "you", "my question") rather than a
# bare judgment word, so a legitimate correction is caught without also
# catching "wrong"/"no" used about something else entirely (a third party, a
# document's claim, conversational filler). Errs toward missing a correction
# over over-counting an unrelated sentence.
CORRECTION_PHRASES: tuple[str, ...] = (
    "that's not what i asked",
    "that is not what i asked",
    "that's not what i meant",
    "that is not what i meant",
    "that's not what i wanted",
    "not what i asked for",
    "i didn't ask for",
    "i did not ask for",
    "you didn't answer that",
    "you did not answer that",
    "you didn't answer my question",
    "you did not answer my question",
    "that doesn't answer my question",
    "that does not answer my question",
    "you misunderstood my question",
    "you misunderstood the question",
    "wrong tool",
    "wrong model",
)

_WHITESPACE_RE = re.compile(r"\s+")
# Strips "..."/'...' spans (non-greedy) so quoted text never contributes a
# false match — a straight-quote-only heuristic (curly quotes are left
# alone deliberately: normalizing them would risk mangling legitimate
# apostrophes in contractions like "that's").
_QUOTED_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]")


def _first_sentence_unquoted(text: str) -> str:
    stripped = _QUOTED_RE.sub(" ", text)
    normalized = _WHITESPACE_RE.sub(" ", stripped).strip().lower()
    if not normalized:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(normalized, maxsplit=1)
    return parts[0].strip()


def looks_like_correction(text: str) -> bool:
    """Whether `text` (a new user turn) reads as an implicit correction of
    the assistant's immediately preceding answer. Checks only the first
    sentence, with quoted spans stripped first (see module docstring)."""
    first_sentence = _first_sentence_unquoted(text or "")
    if not first_sentence:
        return False
    return any(phrase in first_sentence for phrase in CORRECTION_PHRASES)


def correction_tracking_enabled() -> bool:
    """CORRECTION_TRACKING, default ON — see module docstring for why this
    is one of the rare flags that defaults on rather than off."""
    from .settings import bool_setting

    return bool_setting("CORRECTION_TRACKING", True)


def record_if_correction(
    owner: str | None,
    prior_messages: list[dict[str, Any]],
    user_question: str,
) -> None:
    """The hook ask_conversation_impl/ask_conversation_stream call with the
    SAME `prior_messages` they already fetched (before the new user turn was
    persisted) and the new turn's raw question text. A no-op unless tracking
    is enabled, there IS a prior message, that prior message is an assistant
    answer, and `user_question` reads as a correction of it — in every other
    case, records nothing and returns immediately. Never raises for an
    ordinary miss; this is pure measurement, never on the critical path for
    producing an answer.
    """
    if not correction_tracking_enabled():
        return
    if not prior_messages:
        return
    previous = prior_messages[-1]
    if previous.get("role") != "assistant":
        return
    if not looks_like_correction(user_question):
        return

    mode_used = previous.get("mode_used")
    model = previous.get("model") or parse_mode_used(mode_used)[0]
    category = parse_mode_used(mode_used)[1]

    from . import database

    database.record_correction_flag(
        owner=owner,
        message_id=int(previous["id"]),
        model=model,
        mode_used=mode_used,
        category=category,
    )


def _empty_stat() -> dict[str, int | float]:
    return {"flagged": 0, "answers": 0, "correction_rate": 0.0}


def summarize(owner: str | None, days: int) -> dict[str, Any]:
    """Per-model, per-category, and per-lane aggregates: {"by_model": {model:
    stat}, "by_category": {category: stat}, "by_lane": {lane: stat}}, plus an
    "overall" stat across every dimension, where each `stat` is {"flagged",
    "answers", "correction_rate"}. `answers` is every assistant message in
    the window for that dimension (see database.assistant_message_mode_rows)
    — the same "how many answers could this have applied to" denominator
    used for every breakdown, so `correction_rate` means the same thing in
    every row: flags / eligible answers. A NOISY PROXY (see module
    docstring) — callers surfacing this (see self_report.py) must caveat it
    as such, not present it as a verified error rate.
    """
    from . import database

    by_model: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    by_lane: dict[str, dict[str, Any]] = {}
    overall = _empty_stat()

    for row in database.assistant_message_mode_rows(owner, days):
        mode_used = row["mode_used"]
        model = row["model"] or parse_mode_used(mode_used)[0]
        category = parse_mode_used(mode_used)[1]
        lane = lane_from_mode_used(mode_used)
        overall["answers"] += 1
        if model is not None:
            by_model.setdefault(model, _empty_stat())["answers"] += 1
        if category is not None:
            by_category.setdefault(category, _empty_stat())["answers"] += 1
        if lane is not None:
            by_lane.setdefault(lane, _empty_stat())["answers"] += 1

    for entry in database.correction_log_entries(owner, days):
        mode_used = entry["mode_used"]
        model = entry["model"] or parse_mode_used(mode_used)[0]
        category = entry["category"] or parse_mode_used(mode_used)[1]
        lane = lane_from_mode_used(mode_used)
        overall["flagged"] += 1
        if model is not None:
            by_model.setdefault(model, _empty_stat())["flagged"] += 1
        if category is not None:
            by_category.setdefault(category, _empty_stat())["flagged"] += 1
        if lane is not None:
            by_lane.setdefault(lane, _empty_stat())["flagged"] += 1

    for bucket in (by_model, by_category, by_lane, {"_": overall}):
        for stat in bucket.values():
            stat["correction_rate"] = (
                stat["flagged"] / stat["answers"] if stat["answers"] else 0.0
            )

    return {
        "overall": overall,
        "by_model": by_model,
        "by_category": by_category,
        "by_lane": by_lane,
    }
