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
) -> str:
    """Fold older turns into a compact summary via the injected summarizer.

    Returns "" (no summary) when there is nothing to summarize or the summarizer
    yields nothing / fails — the caller then just uses the recent turns verbatim,
    so summarization is always a best-effort enhancement, never a hard dependency.
    """
    transcript = _transcript(older_messages)
    if not transcript:
        return ""
    try:
        return (summarize(transcript) or "").strip()
    except Exception:
        return ""
