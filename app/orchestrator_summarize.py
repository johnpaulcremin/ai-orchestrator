"""Cheap-router-model summarization: folding older conversation turns into a
compact memory summary (summarize_text, used by app/context_summary.py) and
the user-facing conversation TL;DR (summarize_conversation_for_display).
Both are best-effort — any failure (missing key, timeout, provider error)
degrades to '' rather than breaking the caller."""

from __future__ import annotations

import os
from typing import Any

from openai import BadRequestError

from .settings import model_setting

_SUMMARY_PROMPT = (
    "Summarize the earlier part of a conversation into compact notes the "
    "assistant can rely on later. Preserve facts, decisions, names, numbers, and "
    "anything the user might refer back to. Be concise and omit pleasantries.\n\n"
    "Conversation excerpt:\n{text}"
)

# Cap on the transcript fed to the summarizer, to bound cost. When the older
# window is larger than this, keep the TAIL (the most recent of the older turns,
# which are the most relevant) rather than truncating to the oldest.
_SUMMARY_INPUT_CHARS = 24000


def _summary_max_tokens() -> int:
    raw = (os.getenv("SUMMARY_MAX_OUTPUT_TOKENS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 600
    return value if value > 0 else 600


def _run_summary_call(prompt: str) -> str:
    """Shared plumbing for a one-shot, cheap-router-model summarization call.
    Returns '' on any failure (missing key, timeout, provider error) — every
    caller treats summarization as best-effort, never a hard dependency.
    """
    # Local import (not at module level): avoids a circular import with
    # orchestrator.py at load time, and — since it re-resolves the attribute
    # on every call — keeps a test's monkeypatch on orchestrator.get_client
    # effective here regardless of which module get_client is defined in
    # (see app/semantic_cache.py's embed() for the same pattern).
    from .orchestrator import get_client

    try:
        client = get_client()
    except RuntimeError:
        return ""

    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano")
    # Best-effort + on the pre-answer critical path: fail fast (no SDK retries)
    # and a modest timeout, so a slow endpoint can't stall the answer for long.
    timeout_client = client.with_options(timeout=12.0, max_retries=0)

    def _create(**extra: Any) -> object:
        return timeout_client.responses.create(
            model=router_model,
            input=prompt,
            max_output_tokens=_summary_max_tokens(),
            **extra,
        )

    try:
        # Minimal reasoning keeps the summary call cheap, like the router.
        result = _create(reasoning={"effort": "minimal"})
    except BadRequestError:
        try:
            result = _create()
        except Exception:
            return ""
    except Exception:
        return ""

    return (getattr(result, "output_text", None) or "").strip()


def summarize_text(text: str) -> str:
    """Summarize text with the cheap router model. Returns '' on any failure.

    Used to fold older conversation turns into a memory summary. It never raises,
    so a missing key / model error simply omits the summary.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    # Keep the most recent slice of the older window (see _SUMMARY_INPUT_CHARS).
    prompt = _SUMMARY_PROMPT.format(text=clean[-_SUMMARY_INPUT_CHARS:])
    return _run_summary_call(prompt)


_DISPLAY_SUMMARY_PROMPT = (
    "Write a short, user-facing TL;DR of this conversation: the key topics "
    "discussed, any decisions or conclusions reached, and any open questions "
    "still unresolved. Plain prose or a short bulleted list, whichever reads "
    "better. No meta-commentary about being a summary — just the recap "
    "itself.\n\n"
    "Conversation:\n{text}"
)


def summarize_conversation_for_display(messages: list[dict[str, Any]]) -> str:
    """A short, user-facing TL;DR of a whole conversation — backs the 🧾
    Summarize button. Distinct from summarize_text, whose terse, fact-dense
    notes are meant to feed back into the model as context, not to be read
    directly by a person. Returns '' on any failure (missing key, model
    error, or a conversation with no message content to summarize).
    """
    lines = []
    for message in messages:
        role = str(message.get("role", "")).strip().upper()
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    transcript = "\n".join(lines)
    if not transcript:
        return ""
    prompt = _DISPLAY_SUMMARY_PROMPT.format(text=transcript[-_SUMMARY_INPUT_CHARS:])
    return _run_summary_call(prompt)
