"""Rolling conversation summary: folds the turns that have aged out of the
recent window into one compact paragraph, so a long thread stays affordable.

Incremental, not re-summarised from scratch. Given a previous summary, only
the messages that newly aged out since it was computed are folded in, with
one cheap call. Re-summarising the whole older history every turn was the
original approach and it got steadily more expensive the longer a
conversation ran — the exact shape of cost this feature exists to prevent.

The summarizer is injected rather than imported, which keeps this module
free of any provider or orchestrator dependency and makes it trivially
testable with a plain function.

Every failure path returns the previous summary rather than raising or
returning empty: summarization is a best-effort enhancement to context, and
a failed fold must not discard a summary that was already paid for.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        role = str(message.get("role", "")).strip().upper()
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def summarize_conversation(
    older_messages: list[dict[str, Any]],
    summarize: Callable[[str], str],
    previous_summary: str = "",
) -> str:
    """Fold older turns into a compact summary via the injected summarizer.

    When `previous_summary` is given, `older_messages` should be ONLY the
    messages that newly aged out of the recent window since that summary was
    last computed (see database.get_summary_cache / build_context_prompt in
    main.py) — this folds just the new delta into the existing summary with
    one cheap call, instead of re-summarizing the whole older history from
    scratch on every single answer, which is what made a long thread's
    summarizer call grow ever larger (and costlier) turn after turn.

    Returns `previous_summary` (stripped) when there's nothing new to fold in
    or the summarizer yields nothing / fails — summarization is always a
    best-effort enhancement, never a hard dependency, and a failed fold must
    not throw away the summary already paid for.
    """
    transcript = _transcript(older_messages)
    if not transcript:
        return previous_summary.strip()
    prompt = transcript
    if previous_summary.strip():
        prompt = (
            f"Existing summary of earlier context:\n{previous_summary.strip()}\n\n"
            f"New messages to fold in:\n{transcript}"
        )
    try:
        return (summarize(prompt) or "").strip() or previous_summary.strip()
    except Exception:
        return previous_summary.strip()
