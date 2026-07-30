"""Context-assembly helpers shared by the ask/regenerate/edit routes in
app/routers/messages.py: turning a conversation's prior messages (plus any
system prompt, checkpoint summary, and recalled memory) into the prompt
actually sent to the model.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, NamedTuple

from .context_summary import summarize_conversation
from .database import get_summary_cache, set_summary_cache
from .orchestrator import summarize_text


def _summarize_history_enabled() -> bool:
    raw = (os.getenv("SUMMARIZE_HISTORY") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


# The recent-window size a checkpoint fold trims back down to, and the size it
# must grow past before a fold triggers (see _assemble_context_parts). A wide
# gap between them means the "system + summary" prefix sent to the model stays
# byte-identical across many consecutive turns — which is what lets a provider's
# native prompt caching (see providers.call_anthropic's `system` param, and
# OpenAI's automatic prefix caching) actually hit instead of missing every turn
# the way a strict every-turn sliding window would.
_RECENT_WINDOW_MIN = 12
_RECENT_WINDOW_MAX = 24

# A hard token-budget backstop alongside _RECENT_WINDOW_MAX's message-count
# trigger: a handful of very long messages (a pasted log, a large code diff)
# can blow past a reasonable context size long before the verbatim tail hits
# 24 messages. approx_tokens() uses the same chars/4 heuristic as
# orchestrator_summarize.py's _SUMMARY_INPUT_CHARS rather than pulling in a
# real tokenizer dependency — close enough to trigger a fold before the
# window gets expensive, not meant to be an exact count.
_RECENT_WINDOW_TOKEN_MAX = 6000


def _approx_tokens(text: str) -> int:
    return len(text) // 4


class _ContextParts(NamedTuple):
    # Framing + "Instructions for this conversation" + "Summary of earlier
    # messages" — everything that stays byte-identical across consecutive
    # turns between checkpoint folds. Empty string when there's neither a
    # system prompt nor a summary (e.g. a short, freshly-started conversation).
    system_block: str
    # "Conversation history:" + the recent verbatim turns + "Current user
    # question:" + the question itself — changes (grows) every turn.
    recent_and_question: str

    @property
    def full(self) -> str:
        """The single flattened prompt every call site historically got from
        build_context_prompt — byte-identical to the pre-split behavior."""
        if not self.system_block:
            return self.recent_and_question
        return f"{self.system_block}\n\n{self.recent_and_question}"


def _memory_block(memory_snippets: list[str] | None) -> str:
    if not memory_snippets:
        return ""
    lines = [
        "Relevant context from other past conversations (may or may not "
        "actually be relevant here — use your own judgment, and don't "
        "assume the current question is about the same topic unless it "
        "clearly is):",
        *memory_snippets,
    ]
    return "\n".join(lines)


def _library_block(library_snippets: list[str] | None) -> str:
    if not library_snippets:
        return ""
    lines = [
        "Relevant context from your document library (may or may not "
        "actually be relevant here — use your own judgment, and don't "
        "assume the current question is about these documents unless it "
        "clearly is):",
        *library_snippets,
    ]
    return "\n".join(lines)


def _assemble_context_parts(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
    conversation_id: int | None = None,
    memory_snippets: list[str] | None = None,
    library_snippets: list[str] | None = None,
) -> _ContextParts:
    clean_system_prompt = (system_prompt or "").strip()
    memory_block = _memory_block(memory_snippets)
    library_block = _library_block(library_snippets)

    if (
        not prior_messages
        and not clean_system_prompt
        and not memory_block
        and not library_block
    ):
        return _ContextParts(system_block="", recent_and_question=current_question)

    if not prior_messages:
        # No history yet, but custom instructions and/or recalled memory/
        # library context exist: skip the conversation-history framing
        # entirely rather than describing history that doesn't exist. This
        # is actually the highest-value case for memory/the library — a
        # BRAND NEW conversation about a topic already covered elsewhere (or
        # in an uploaded document) has nothing else to draw on.
        blocks = []
        if clean_system_prompt:
            blocks.append(f"Instructions for this conversation:\n{clean_system_prompt}")
        if memory_block:
            blocks.append(memory_block)
        if library_block:
            blocks.append(library_block)
        return _ContextParts(
            system_block="\n\n".join(blocks),
            recent_and_question=f"Current user question:\n{current_question}",
        )

    # Checkpoint-based window (conversation_id given, summarization on): the
    # "older" boundary only advances once the verbatim tail grows past
    # _RECENT_WINDOW_MAX, folding it back down to _RECENT_WINDOW_MIN in one
    # go — unlike a strict last-12 slice, this keeps the boundary (and so the
    # summary text) fixed across most turns, which is the whole point (see
    # _RECENT_WINDOW_MIN's docstring above). Every other caller (regenerate/
    # edit, which omit conversation_id, or summarization disabled) keeps the
    # original fixed last-12-messages behavior — those are one-off rebuilds,
    # not part of the steady-state per-turn loop this optimizes.
    if conversation_id is not None and _summarize_history_enabled():
        cached = get_summary_cache(conversation_id)
        checkpoint = (
            min(int(cached["older_count"]), len(prior_messages)) if cached else 0
        )
        summary = str(cached["summary"]) if cached else ""
        recent_messages = prior_messages[checkpoint:]
        older_messages = prior_messages[:checkpoint]

        recent_tokens = sum(
            _approx_tokens(str(m.get("content", ""))) for m in recent_messages
        )
        # The token check only fires once there's more than _RECENT_WINDOW_MIN
        # messages to fold down to — otherwise recent_messages[:-_RECENT_WINDOW_MIN]
        # below would be empty (nothing to fold) and this would just re-trigger
        # every turn without ever reducing recent_tokens.
        if len(recent_messages) > _RECENT_WINDOW_MAX or (
            len(recent_messages) > _RECENT_WINDOW_MIN
            and recent_tokens > _RECENT_WINDOW_TOKEN_MAX
        ):
            summarizer = summarize if summarize is not None else summarize_text
            to_fold = recent_messages[:-_RECENT_WINDOW_MIN]
            summary = summarize_conversation(
                to_fold, summarizer, previous_summary=summary
            )
            checkpoint += len(to_fold)
            set_summary_cache(conversation_id, checkpoint, summary)
            recent_messages = prior_messages[checkpoint:]
            older_messages = prior_messages[:checkpoint]
    else:
        recent_messages = prior_messages[-12:]
        older_messages = prior_messages[:-12]
        summary = ""
        if older_messages and _summarize_history_enabled():
            summarizer = summarize if summarize is not None else summarize_text
            summary = summarize_conversation(older_messages, summarizer)

    # older_messages existing but summary still empty means summarization was
    # needed and attempted but yielded nothing usable (disabled, no cached
    # fallback, or a swallowed failure deep in summarize_text /
    # summarize_conversation) — the model is missing that older context, so it
    # must not be told to assume it has the full picture.
    context_incomplete = bool(older_messages) and not summary

    system_lines = [
        "You are continuing a saved conversation.",
        "Use the conversation history below when it is relevant.",
        (
            "Some earlier messages in this conversation happened before the "
            "history shown below and could not be summarized here — if the "
            "user asks about something from that period, say you don't have "
            "it rather than guessing or claiming there is no earlier history."
            if context_incomplete
            else "Do not claim you lack context if the answer is present in the history."
        ),
    ]

    if clean_system_prompt:
        system_lines.extend(
            ["", "Instructions for this conversation:", clean_system_prompt]
        )

    if summary:
        system_lines.extend(["", "Summary of earlier messages:", summary])

    if memory_block:
        system_lines.extend(["", memory_block])

    if library_block:
        system_lines.extend(["", library_block])

    recent_lines = ["Conversation history:"]

    for message in recent_messages:
        role = str(message.get("role", "unknown")).strip()
        content = str(message.get("content", "")).strip()

        if not content:
            continue

        recent_lines.append(f"{role.upper()}: {content}")

    recent_lines.extend(["", "Current user question:", current_question])

    return _ContextParts(
        system_block="\n".join(system_lines),
        recent_and_question="\n".join(recent_lines),
    )


def build_context_prompt(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
    conversation_id: int | None = None,
    memory_snippets: list[str] | None = None,
    library_snippets: list[str] | None = None,
) -> str:
    return _assemble_context_parts(
        prior_messages,
        current_question,
        system_prompt,
        summarize,
        conversation_id,
        memory_snippets,
        library_snippets,
    ).full


def build_context_prompt_with_cache_split(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
    conversation_id: int | None = None,
    memory_snippets: list[str] | None = None,
    library_snippets: list[str] | None = None,
) -> tuple[str, str | None, str]:
    """Same full prompt build_context_prompt returns, plus (when there's a
    system-prompt/summary block worth the split) that block isolated as
    `cacheable_system` — for threading to a provider integration that can
    cache a stable prefix natively (currently: Anthropic's `system` param with
    a cache_control breakpoint; see providers.call_anthropic). `cacheable_system`
    is None when there's nothing to isolate (a fresh conversation with no
    instructions), in which case `remainder` is just `full` again.

    The THIRD return value, `remainder`, is `full` with `cacheable_system`
    (and the blank line after it) stripped off the front — the caller MUST
    send Anthropic `remainder`, not `full`, whenever it also sends
    `cacheable_system` via the native `system` param; sending `full` too would
    duplicate that same text into the user turn, doubling those tokens
    instead of caching them.

    `memory_snippets` (see app/memory.py) is recalled cross-conversation
    context, and `library_snippets` (see app/rag_library.py) is recalled
    document-library context, both folded into the cacheable system_block
    alongside instructions/summary when present — same caching treatment,
    since it's stable for this one turn regardless of provider.
    """
    parts = _assemble_context_parts(
        prior_messages,
        current_question,
        system_prompt,
        summarize,
        conversation_id,
        memory_snippets,
        library_snippets,
    )
    return parts.full, (parts.system_block or None), parts.recent_and_question


# How many recent turns the ambiguity classifier sees — enough to catch a
# "this"/"that" referring back a turn or two, small enough to stay a cheap
# addition to the same classifier call rather than a meaningfully bigger one.
_AMBIGUITY_HISTORY_TURNS = 4


def build_recent_history_snippet(
    prior_messages: list[dict[str, Any]], turns: int = _AMBIGUITY_HISTORY_TURNS
) -> str:
    """A short "ROLE: content" snippet of the last few turns, for the router's
    ambiguity check only (see routing.decide_route) — never used to build the
    actual answering prompt. Each line capped so one long past message can't
    blow up the classifier prompt; empty string when there's no history yet,
    the same "nothing to be ambiguous against" case the classifier treats as
    never ambiguous."""
    lines = []
    for message in prior_messages[-turns:]:
        role = str(message.get("role", "unknown")).strip()
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{role.upper()}: {content[:300]}")
    return "\n".join(lines)
