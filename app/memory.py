"""Cross-conversation memory: an opt-in extra-context layer that lets a
conversation recall relevant exchanges from the same owner's OTHER past
conversations, via embedding similarity — the same "no vector DB,
brute-force cosine scan, OpenAI embeddings" approach as app/semantic_cache.py
(embed/_cosine_similarity are reused from there directly, not duplicated).

Deliberately a different concern from semantic_cache: that module serves a
CACHED ANSWER outright, only for a context-free (no history) question, in an
exact mode+model+owner scope — a narrow, all-or-nothing guarantee. This
module never serves an answer; it only INJECTS a handful of relevant past
exchanges into the prompt as extra context for the model to use its own
judgment on — the same way a conversation's own "Summary of earlier
messages" already works (see app/routers/messages.py's
_assemble_context_parts), just reaching across conversation boundaries
instead of within one. That's also why the match threshold here is looser
than semantic_cache's: a false positive just adds a possibly-irrelevant
snippet, not a wrong answer served outright.

Scoped to conversation-based asks only (ask_conversation/
ask_conversation_stream in app/routers/messages.py) — not the stateless
/v1/ask, and not regenerate/edit — since "recall from past conversations"
only makes sense once conversations exist to recall from and write into.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from . import database
from .semantic_cache import _cosine_similarity, embed
from .settings import bool_setting

__all__ = [
    "clear",
    "embed",
    "format_snippet",
    "max_entries",
    "memory_enabled",
    "recall",
    "remember",
    "stats",
    "threshold",
    "top_k",
]


def memory_enabled() -> bool:
    """Opt-in: CROSS_CONVERSATION_MEMORY=true (env, or a saved Settings
    override — same override > env > default chain as any other toggle).
    Off by default: this changes what gets folded into every conversation's
    prompt, the same class of behavior change as SEMANTIC_CACHE, so it
    needs an explicit opt-in rather than being silently on."""
    return bool_setting("CROSS_CONVERSATION_MEMORY", False)


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def threshold() -> float:
    """Minimum cosine similarity to count as relevant enough to inject.
    Looser than semantic_cache's 0.96 — see module docstring on why a false
    positive here is a materially cheaper failure mode."""
    value = _float_env("MEMORY_THRESHOLD", 0.75)
    return value if 0.0 < value <= 1.0 else 0.75


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def top_k() -> int:
    """How many past exchanges to inject at most, when relevant ones exist."""
    value = _int_env("MEMORY_TOP_K", 3)
    return value if value > 0 else 3


def max_entries() -> int:
    """Cap on stored entries PER OWNER (unlike semantic_cache's single global
    cap — memory is meant to grow with one user's real conversation history
    over time, not share one budget across every owner on a multi-user
    deployment). Kept modest since recall is a brute-force scan over every
    stored entry for that owner."""
    value = _int_env("MEMORY_MAX_ENTRIES", 500)
    return value if value > 0 else 500


# Longest an answer is kept verbatim in a recalled snippet — long enough to
# carry real information, short enough that a handful of recalled answers
# doesn't balloon the prompt the way including each one in full would.
_ANSWER_SNIPPET_CHARS = 400


def format_snippet(entry: dict[str, Any]) -> str:
    """A recalled entry as the "[From ...]\\nQ: ...\\nA: ..." text folded
    into the prompt — the answer truncated, the question kept in full
    (short and the more important half for the model to judge relevance
    from).

    PROVENANCE: prefixed with the source conversation's title and date
    (from database.memory_list's join — see that function's docstring).
    This matters because the similarity threshold alone cannot catch every
    wrong match: the semantic-cache/memory eval (see evals/README.md's
    decision-gate audit) measured that a changed-name or changed-date
    confusable ("email Priya" vs "email Devon", one date vs another) can
    clear MEMORY_THRESHOLD (0.75) with a near-identical embedding — an
    entity swap is nearly invisible to embedding similarity, so there is
    no threshold that reliably separates these two cases. The source
    title/date is the model's own remaining signal for catching a mismatch
    the embedding math didn't (paired with _memory_block's caution in
    app/context_builder.py, which spells out that a recalled snippet may
    concern a different person/project/date) — this doesn't fix the
    recall step, it gives the model what it needs to exercise its own
    judgment on what recall got wrong.
    """
    question = str(entry.get("question", "")).strip()
    answer = str(entry.get("answer", "")).strip()
    if len(answer) > _ANSWER_SNIPPET_CHARS:
        answer = answer[:_ANSWER_SNIPPET_CHARS].rstrip() + "..."
    title = (
        str(entry.get("conversation_title") or "").strip() or "an untitled conversation"
    )
    date = str(entry.get("created_at", "")).strip().split(" ")[0]  # date part only
    when = f" on {date}" if date else ""
    return f'[From "{title}"{when}]\nQ: {question}\nA: {answer}'


def recall(
    vector: list[float] | None, owner: str | None, exclude_conversation_id: int
) -> list[dict[str, Any]]:
    """Up to top_k() past exchanges from this owner's OTHER conversations,
    above threshold(), best match first. [] if memory is off, `vector` is
    None (embedding failed, or the caller skipped it), or nothing clears the
    bar. Never raises: a broken recall must not break answering."""
    if not memory_enabled() or vector is None:
        return []
    try:
        candidates = database.memory_list(owner, exclude_conversation_id)
    except sqlite3.Error:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in candidates:
        try:
            candidate_vector = json.loads(str(row["embedding"]))
        except (TypeError, ValueError):
            continue
        score = _cosine_similarity(vector, candidate_vector)
        if score >= threshold():
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _score, row in scored[: top_k()]]


def remember(
    owner: str | None,
    conversation_id: int,
    question: str,
    answer: str,
    vector: list[float] | None,
) -> None:
    """Store this turn's embedding for future cross-conversation recall.

    No-op if memory is off, there's no answer text, or `vector` is None
    (embedding failed during the recall() call this reuses — never embeds a
    second time just to write, same pattern as semantic_cache.put). Never
    raises: a failed write must not fail the request.
    """
    if not memory_enabled() or not (answer or "").strip() or vector is None:
        return
    try:
        database.memory_put(
            owner, conversation_id, question, answer, json.dumps(vector)
        )
        cap = max_entries()
        if cap:
            count = database.memory_count(owner)
            if count > cap:
                database.memory_delete_oldest(owner, count - cap)
    except sqlite3.Error:
        return


def clear() -> int:
    try:
        return database.memory_clear()
    except sqlite3.Error:
        return 0


def stats() -> dict[str, Any]:
    try:
        entries = database.memory_total_count()
    except sqlite3.Error:
        entries = 0
    return {
        "enabled": memory_enabled(),
        "entries": entries,
        "threshold": threshold(),
        "top_k": top_k(),
        "max_entries": max_entries(),
    }
