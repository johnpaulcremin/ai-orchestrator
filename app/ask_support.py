"""Small ask/regenerate helpers shared by app/routers/messages.py: title
generation, model-pin resolution, memory recall, and the context-free check
used to gate the semantic cache.
"""

from __future__ import annotations

import time

from . import memory
from .schemas import AskRequest, Mode


def _is_generic_title(title: str) -> bool:
    # The default titles the UI and API create; a first message replaces them.
    clean_title = title.strip().lower()
    return clean_title in {
        "untitled conversation",
        "new ai workbench conversation",
    }


def _title_from_question(question: str) -> str:
    clean_question = " ".join(question.strip().split())

    if not clean_question:
        return "Untitled conversation"

    max_len = 70
    if len(clean_question) <= max_len:
        return clean_question

    return f"{clean_question[:max_len].rstrip()}..."


# Pin values that mean "use this tier" rather than "force this exact model".
_TIER_PINS = {"budget", "fast", "smart"}


def _is_context_free(prior_messages: list[dict], conversation: dict) -> bool:
    """True exactly when build_context_prompt_with_cache_split's assembled
    prompt would be nothing but the bare question — no prior turns, no
    custom system prompt — the one shape orchestrator.run_orchestrator's
    `context_free` param (see its docstring) allows the semantic cache to
    even look at. A conversation with any history or instructions is never
    context-free, since the assembled prompt then carries that context too."""
    return not prior_messages and not (conversation.get("system_prompt") or "").strip()


def _recall_memory(
    question: str, owner: str | None, conversation_id: int
) -> tuple[list[float] | None, list[str], int]:
    """(vector, snippets, duration_ms) for a new turn on this conversation:
    the embedded `question` (None if memory is off or embedding failed) and
    up to memory.top_k() formatted snippets recalled from the owner's OTHER
    conversations (see app/memory.py) — or ([], []) when memory is off, so
    the embed call is skipped entirely rather than computed and discarded.
    `vector` is returned so the caller can reuse it for memory.remember()
    after answering, instead of embedding the same question twice.

    `duration_ms` is how long THIS call took — folded into
    orchestrator.run_orchestrator/stream_orchestrator's `pre_stage_timings`
    (see telemetry.StageTimer) so the per-stage latency log reflects a stage
    that happens entirely before the orchestrator is ever invoked.
    """
    started = time.perf_counter()
    if not memory.memory_enabled():
        return None, [], int((time.perf_counter() - started) * 1000)
    vector = memory.embed(question)
    hits = memory.recall(vector, owner, exclude_conversation_id=conversation_id)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return vector, [memory.format_snippet(hit) for hit in hits], duration_ms


def _memory_stage_timing(memory_ms: int) -> dict[str, int] | None:
    """Only surfaces a `memory_embed` stage when memory is actually enabled
    — otherwise `_recall_memory` did no real work, and logging a
    `memory_embed=0ms` entry on every single request would just be noise."""
    if not memory.memory_enabled():
        return None
    return {"memory_embed": memory_ms}


def _pinned_ask_request(
    conversation: dict, question: str, req: AskRequest
) -> AskRequest:
    """Apply the conversation's model pin (if any) to a new question.

    A pin fully determines routing for normal asks: a 'fast'/'smart' pin forces
    that tier; any other value forces that exact model (bypassing the router and
    cache, like switch-model) with the generous smart-tier budget — independent
    of the request's mode, which the UI disables while pinned. No pin -> the
    request's own mode (and any client-forced `model`) is used, same as `/v1/ask`.
    """
    pin = (conversation.get("pinned_model") or "").strip()
    if pin in _TIER_PINS:
        return AskRequest(
            question=question,
            mode=Mode(pin),
            no_cache=req.no_cache,
            images=req.images,
            files=req.files,
            research=req.research,
        )
    if pin:
        return AskRequest(
            question=question,
            mode=Mode.smart,
            no_cache=req.no_cache,
            model=pin,
            images=req.images,
            files=req.files,
            research=req.research,
        )
    return AskRequest(
        question=question,
        mode=req.mode,
        no_cache=req.no_cache,
        model=req.model,
        images=req.images,
        files=req.files,
        research=req.research,
    )
