"""Small ask/regenerate helpers shared by app/routers/messages.py: title
generation, model-pin resolution, memory recall, and the context-free check
used to gate the semantic cache.

Document-library recall deliberately does NOT live here alongside memory
recall, though it once did: it is gated on the task category the router's
classifier produces, so it can only run after routing, inside the
orchestrator — see orchestrator._recall_library_context and
rag_library.recall.
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
) -> tuple[list[float] | None, list[str], list[dict], int]:
    """(vector, snippets, sources, duration_ms) for a new turn on this
    conversation: the embedded `question` (None if memory is off or
    embedding failed), up to memory.top_k() formatted snippets recalled
    from the owner's OTHER conversations (see app/memory.py), and `sources`
    (the answer's `memory_sources` field — [] when nothing was recalled)
    summarizing which past conversation(s) each snippet came from — or
    ([], []) for both when memory is off, so the embed call is skipped
    entirely rather than computed and discarded. `vector` is returned so
    the caller can reuse it for memory.remember() after answering, instead
    of embedding the same question twice.

    `duration_ms` is how long THIS call took — folded into
    orchestrator.run_orchestrator/stream_orchestrator's `pre_stage_timings`
    (see telemetry.StageTimer) so the per-stage latency log reflects a stage
    that happens entirely before the orchestrator is ever invoked.
    """
    started = time.perf_counter()
    if not memory.memory_enabled():
        return None, [], [], int((time.perf_counter() - started) * 1000)
    vector = memory.embed(question)
    hits = memory.recall(vector, owner, exclude_conversation_id=conversation_id)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return (
        vector,
        [memory.format_snippet(hit) for hit in hits],
        memory.summarize_sources(hits),
        duration_ms,
    )


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

    `Mode.workflow` is the ONE mode a pin does not overwrite. A pin is a
    statement about which MODEL answers; it is not a veto on the SHAPE of the
    answer. Every other mode here names a single-shot tier, so replacing it with
    the pin's own tier loses nothing — but `workflow` names a multi-step answer,
    and rewriting it to `Mode.smart` silently turned a workflow request into a
    single-shot one. That mattered because regenerate.py and edit.py decide
    whether to run a workflow by reading the request this function RETURNS, so
    on a pinned conversation the decision was made after the evidence for it had
    been erased: `$ Retry as workflow` on a truncated answer dispatched an
    ordinary answer at the smart tier's 4000-token cap — the very ceiling that
    had just cut the answer off. The remedy was inert exactly where it was
    offered. (`/v1/ask` was unaffected: it branches on the caller's own
    `req.mode` before ever calling this.)

    A MODEL pin is still honoured on that path — it rides along as the forced
    model, so every step runs on the pinned model. A TIER pin is not, and cannot
    be: a workflow routes each step by its own category and `run_workflow` has
    no notion of a tier floor to apply (it reads `req.model` and nothing else).
    Said plainly rather than papered over — the caller asked for a workflow and
    gets one, which is the useful outcome; inventing a per-step tier override to
    carry the pin would be a separate feature, not this fix.
    """
    pin = (conversation.get("pinned_model") or "").strip()

    if req.mode == Mode.workflow:
        return AskRequest(
            question=question,
            mode=Mode.workflow,
            no_cache=req.no_cache,
            # A model pin becomes the forced model; a tier pin has nothing to
            # force, so the caller's own `model` (usually None) stands.
            model=pin if pin and pin not in _TIER_PINS else req.model,
            images=req.images,
            files=req.files,
            research=req.research,
        )

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
