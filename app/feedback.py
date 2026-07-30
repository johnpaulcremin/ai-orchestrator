"""Quality feedback: a per-answer 👍/👎 rating (see app/database.py's
`messages.feedback`/`feedback_log`) that validates the routing premise ("the
cheap model was good enough") against lived outcomes, not just cost — this
app measures spend everywhere but, until now, quality nowhere.

Always on — no feature flag, same reasoning as bookmarks/favorites: rating
an answer is zero-cost, entirely local, and passive (nothing happens unless
the user clicks 👍/👎), unlike WEB_SEARCH/CODE_EXECUTION/etc., which spend
real money or reach an external service and so need an explicit opt-in.

Deliberately NO implicit signals: a regenerate or an edited-and-resent
message is NOT auto-counted as a negative rating anywhere in this module or
its callers. Mixing an explicit click with an inferred one would make the
dataset ambiguous — a regenerate can mean "that answer was wrong" just as
easily as "let's see a different angle" — so the dataset stays limited to
what a user actually clicked, which is the only thing worth trusting it
for. Likewise, deliberately no model-switching automation off the back of
these stats: a human reads GET /v1/feedback/summary and decides whether to
re-point the model map (see app/settings.py); automating that decision is a
real follow-up, not something to bolt on half-considered alongside the
rating mechanism itself. And no per-user leaderboards — this is a
per-model/per-category/per-lane operator signal, not a scoreboard about
which callers rate what.
"""

from __future__ import annotations

from typing import Any

from . import database


def parse_mode_used(mode_used: str | None) -> tuple[str | None, str | None]:
    """(model, category) best-effort parsed from a persisted mode_used
    string, e.g.:
      "forced:claude-sonnet-5"   -> ("claude-sonnet-5", None)
      "auto->free:groq/llama-3" -> ("groq/llama-3", None)
      "auto->fast:coding"       -> (None, "coding")
      "auto->smart"             -> (None, None)

    Only the forced/free-lane shapes embed a concrete model name in
    mode_used itself; a bare "auto->fast"/"auto->smart:coding" shape
    doesn't — the model that actually answered isn't part of mode_used for
    ordinary auto-routed traffic, which is why messages.model exists
    (see database.py's migration comment) as the real source for that case;
    this function is a fallback for messages persisted before that column
    existed, or where it's otherwise unset.
    """
    if not mode_used:
        return None, None
    if mode_used.startswith("forced:"):
        return mode_used[len("forced:") :] or None, None
    if mode_used.startswith("auto->free:"):
        return mode_used[len("auto->free:") :] or None, None
    if mode_used.startswith("auto->"):
        rest = mode_used[len("auto->") :]
        if ":" in rest:
            _tier, category = rest.split(":", 1)
            return None, category or None
        return None, None
    return None, None


def lane_from_mode_used(mode_used: str | None) -> str | None:
    """ "free" | "budget" | "fast" | "smart" | "forced" | None — the coarse
    routing lane a mode_used string reflects, for the "is the free lane
    costing quality" comparison GET /v1/feedback/summary surfaces as its
    headline number."""
    if not mode_used:
        return None
    if mode_used.startswith("auto->free:"):
        return "free"
    if mode_used.startswith("forced:"):
        return "forced"
    if mode_used.startswith("auto->"):
        rest = mode_used[len("auto->") :].split(":", 1)[0]
        return rest or None
    return None


def _empty_stat() -> dict[str, int | float]:
    return {"answers_rated": 0, "up": 0, "down": 0, "down_rate": 0.0}


def _bump(bucket: dict[str, dict[str, Any]], key: str | None, verdict: int) -> None:
    if key is None:
        return
    stat = bucket.setdefault(key, _empty_stat())
    stat["answers_rated"] += 1
    if verdict == 1:
        stat["up"] += 1
    else:
        stat["down"] += 1
    stat["down_rate"] = stat["down"] / stat["answers_rated"]


def summarize(owner: str | None, days: int) -> dict[str, Any]:
    """Per-model, per-category, and per-lane aggregates from feedback_log
    over the last `days` days: {"by_model": {model: stat}, "by_category":
    {category: stat}, "by_lane": {lane: stat}}, where each `stat` is
    {"answers_rated", "up", "down", "down_rate"}.

    Counts LEDGER ROWS (set/change events, clears already excluded by
    feedback_log_entries), not distinct messages — a caller who rates an
    answer 👍 then changes their mind to 👎 contributes two rows in the
    window, same "ledger, not current state" semantics as the rest of
    feedback_log. An entry with no resolvable model/category is simply
    omitted from that one breakdown (see parse_mode_used) — it still
    counts in whichever breakdown(s) it DOES resolve for.
    """
    entries = database.feedback_log_entries(owner, days)

    by_model: dict[str, dict[str, Any]] = {}
    by_category: dict[str, dict[str, Any]] = {}
    by_lane: dict[str, dict[str, Any]] = {}

    for entry in entries:
        verdict = int(entry["verdict"])
        model = entry["model"] or parse_mode_used(entry["mode_used"])[0]
        category = entry["category"] or parse_mode_used(entry["mode_used"])[1]
        _bump(by_model, model, verdict)
        _bump(by_category, category, verdict)
        _bump(by_lane, lane_from_mode_used(entry["mode_used"]), verdict)

    return {"by_model": by_model, "by_category": by_category, "by_lane": by_lane}
