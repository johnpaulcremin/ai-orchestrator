"""Message-level and conversation-scoped ask/regenerate/edit/continue
endpoints, plus the context-building helpers they share. See
app/routers/conversations.py for conversation CRUD, and app/routers/ask.py
for the stateless (context-free) /v1/ask, /v1/compare, /v1/estimate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Any, NamedTuple

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import memory
from ..actions import post_webhook
from ..auth import current_owner
from ..context_summary import summarize_conversation
from ..database import (
    add_message,
    append_to_message,
    claim_pending_action,
    delete_message,
    delete_messages_after,
    delete_messages_from,
    get_conversation,
    get_message,
    get_summary_cache,
    list_bookmarked_messages,
    list_messages,
    set_action_status,
    set_message_bookmarked,
    set_summary_cache,
    update_conversation_title,
)
from ..orchestrator import run_orchestrator, stream_orchestrator, summarize_text
from ..ratelimit import limiter, rate_limit_value
from ..schemas import (
    ActionConfirmRequest,
    ActionResult,
    AskRequest,
    AskResponse,
    BookmarkedMessage,
    FileAttachment,
    MessageBookmark,
    MessageOut,
    MessageRestoreRequest,
    Mode,
    RegenerateRequest,
)
from .deps import (
    _encode_action,
    _encode_code_results,
    _encode_fact_checks,
    _encode_files,
    _encode_images,
    _encode_sources,
    _owned_or_404,
    router,
)


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


def _assemble_context_parts(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
    conversation_id: int | None = None,
    memory_snippets: list[str] | None = None,
) -> _ContextParts:
    clean_system_prompt = (system_prompt or "").strip()
    memory_block = _memory_block(memory_snippets)

    if not prior_messages and not clean_system_prompt and not memory_block:
        return _ContextParts(system_block="", recent_and_question=current_question)

    if not prior_messages:
        # No history yet, but custom instructions and/or recalled memory
        # exist: skip the conversation-history framing entirely rather than
        # describing history that doesn't exist. This is actually the
        # highest-value case for memory — a BRAND NEW conversation about a
        # topic already covered elsewhere has nothing else to draw on.
        blocks = []
        if clean_system_prompt:
            blocks.append(f"Instructions for this conversation:\n{clean_system_prompt}")
        if memory_block:
            blocks.append(memory_block)
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

        if len(recent_messages) > _RECENT_WINDOW_MAX:
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
) -> str:
    return _assemble_context_parts(
        prior_messages,
        current_question,
        system_prompt,
        summarize,
        conversation_id,
        memory_snippets,
    ).full


def build_context_prompt_with_cache_split(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
    conversation_id: int | None = None,
    memory_snippets: list[str] | None = None,
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
    context, folded into the cacheable system_block alongside instructions/
    summary when present — same caching treatment, since it's stable for
    this one turn regardless of provider.
    """
    parts = _assemble_context_parts(
        prior_messages,
        current_question,
        system_prompt,
        summarize,
        conversation_id,
        memory_snippets,
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
) -> tuple[list[float] | None, list[str]]:
    """(vector, snippets) for a new turn on this conversation: the embedded
    `question` (None if memory is off or embedding failed) and up to
    memory.top_k() formatted snippets recalled from the owner's OTHER
    conversations (see app/memory.py) — or ([], []) when memory is off, so
    the embed call is skipped entirely rather than computed and discarded.
    `vector` is returned so the caller can reuse it for memory.remember()
    after answering, instead of embedding the same question twice.
    """
    if not memory.memory_enabled():
        return None, []
    vector = memory.embed(question)
    hits = memory.recall(vector, owner, exclude_conversation_id=conversation_id)
    return vector, [memory.format_snippet(hit) for hit in hits]


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


@router.get("/v1/bookmarks", response_model=list[BookmarkedMessage])
def bookmarks(owner: str | None = Depends(current_owner)):
    """Every bookmarked message across this owner's conversations, newest
    first, so a bookmark set on any one conversation is reviewable in one
    place instead of only visible while that conversation happens to be
    open.
    """
    return list_bookmarked_messages(owner)


@router.get(
    "/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
def conversation_messages(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    _owned_or_404(conversation_id, owner)
    return list_messages(conversation_id)


@router.delete("/v1/conversations/{conversation_id}/messages/{message_id}")
def remove_message(
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Delete a single message (either role) without touching any other
    message — distinct from regenerate/edit, which both replace or discard
    a range of messages and produce a fresh answer."""
    _owned_or_404(conversation_id, owner)
    deleted = delete_message(conversation_id, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"status": "deleted", "message_id": message_id}


@router.post(
    "/v1/conversations/{conversation_id}/messages/restore",
    response_model=MessageOut,
)
def restore_message(
    conversation_id: int,
    req: MessageRestoreRequest,
    owner: str | None = Depends(current_owner),
):
    """Recreate a single message (fresh id, no model call) in this
    conversation — the backing endpoint for Undo after deleting a message.
    Same fidelity as Import, attachments included."""
    _owned_or_404(conversation_id, owner)
    return add_message(
        conversation_id=conversation_id,
        role=req.role,
        content=req.content,
        mode_used=req.mode_used,
        notes=req.notes,
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        cost_usd=req.cost_usd,
        cached=req.cached,
        sources=_encode_sources(req.sources),
        truncated=req.truncated,
        code_results=_encode_code_results(req.code_results),
        fact_checks=_encode_fact_checks(req.fact_checks),
        images=_encode_images(req.images),
        files=_encode_files(req.files),
    )


@router.put(
    "/v1/conversations/{conversation_id}/messages/{message_id}/bookmark",
    response_model=MessageOut,
)
def bookmark_message(
    conversation_id: int,
    message_id: int,
    req: MessageBookmark,
    owner: str | None = Depends(current_owner),
):
    """Bookmark/unbookmark a single message — a marker on one turn, distinct
    from favoriting the whole conversation."""
    _owned_or_404(conversation_id, owner)
    updated = set_message_bookmarked(conversation_id, message_id, req.bookmarked)
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


def _continuation_prompt(prior_content: str) -> str:
    return (
        "Continue your previous answer in this conversation EXACTLY where it "
        "left off. Do not repeat any part of it, and do not add a preamble, "
        "acknowledgement, or restated heading — your reply must pick up "
        "mid-sentence (or mid-code) exactly as if the text below never "
        "stopped.\n\n"
        "Your answer so far, cut off mid-way:\n"
        f"{prior_content}"
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/continue",
    response_model=MessageOut,
)
@limiter.limit(rate_limit_value)
def continue_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Resume a message that got cut off at max_output_tokens.

    Non-streaming by design (unlike ask/regenerate/edit): a continuation is a
    short, occasional follow-up action, not the primary answering path, so a
    second streaming implementation isn't worth the added surface here. The
    continuation is appended to the SAME message row rather than creating a
    new one — from the user's point of view they asked one question and got
    one (possibly multi-part) answer.
    """
    conversation = _owned_or_404(conversation_id, owner)
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "assistant":
        raise HTTPException(
            status_code=400, detail="Only an assistant message can be continued"
        )
    if not target.get("truncated"):
        raise HTTPException(status_code=400, detail="Message was not truncated")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=_continuation_prompt(str(target["content"])),
        system_prompt=conversation.get("system_prompt"),
    )
    base_req = AskRequest(question=context_question, mode=Mode.auto, no_cache=True)
    contextual_req = _pinned_ask_request(conversation, context_question, base_req)

    result = run_orchestrator(contextual_req, owner=owner)

    if not result.answer.strip():
        raise HTTPException(
            status_code=502, detail=result.notes or "Continuation failed"
        )

    updated = append_to_message(
        conversation_id=conversation_id,
        message_id=message_id,
        additional_content=result.answer,
        truncated=result.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


@router.post("/v1/conversations/{conversation_id}/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask_conversation(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

    prior_messages = list_messages(conversation_id)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
        images=_encode_images(req.images),
        files=_encode_files(req.files),
    )

    memory_vector, memory_snippets = _recall_memory(
        req.question, owner, conversation_id
    )

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    # Route on the new user turn, not the assembled context prompt.
    result = run_orchestrator(
        contextual_req,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | context_messages={len(prior_messages)}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
        code_results=result.code_results,
        fact_checks=result.fact_checks,
        truncated=result.truncated,
    )

    # Only persist a real answer: an empty/failed reply (auth error, rate limit,
    # all fallbacks exhausted) must not write an empty assistant bubble. The user
    # turn is already saved and the failure is returned to the client in `notes`.
    if response.answer.strip():
        add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response.answer,
            mode_used=response.mode_used,
            notes=response.notes,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            cached=response.cached,
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
            fact_checks=_encode_fact_checks(response.fact_checks),
        )
        memory.remember(
            owner, conversation_id, req.question, response.answer, memory_vector
        )

    return response


@router.post("/v1/conversations/{conversation_id}/ask/stream")
@limiter.limit(rate_limit_value)
def ask_conversation_stream(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

    prior_messages = list_messages(conversation_id)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
        images=_encode_images(req.images),
        files=_encode_files(req.files),
    )

    memory_vector, memory_snippets = _recall_memory(
        req.question, owner, conversation_id
    )

    context_question, cacheable_system, anthropic_question = (
        build_context_prompt_with_cache_split(
            prior_messages=prior_messages,
            current_question=req.question,
            system_prompt=conversation.get("system_prompt"),
            conversation_id=conversation_id,
            memory_snippets=memory_snippets or None,
        )
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    context_note = f"context_messages={len(prior_messages)}"

    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
        cacheable_system=cacheable_system,
        anthropic_question=anthropic_question,
        context_free=_is_context_free(prior_messages, conversation),
        remember_memory=True,
        memory_question=req.question,
        memory_vector=memory_vector,
    )


def _stream_and_persist(
    conversation_id: int,
    contextual_req: AskRequest,
    context_note: str,
    replace_after_id: int | None = None,
    routing_question: str | None = None,
    owner: str | None = None,
    edit_message_id: int | None = None,
    edit_question: str | None = None,
    edit_images: list[str] | None = None,
    edit_files: list[FileAttachment] | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
    context_free: bool = False,
    remember_memory: bool = False,
    memory_question: str | None = None,
    memory_vector: list[float] | None = None,
) -> StreamingResponse:
    """Stream an orchestrator response as SSE and persist the assistant message.

    Shared by the ask-stream, regenerate-stream, and edit-stream endpoints.
    When `replace_after_id` is set (regenerate), the previous answer(s) after
    that message are deleted only on a successful `done` — right before the
    new answer is stored — so a failed or aborted regeneration leaves the old
    answer intact. `edit_message_id` (edit) works the same way but ALSO
    replaces the edited user message itself: on success, that message and
    everything after it is deleted and a fresh user message (`edit_question`/
    `edit_images`/`edit_files`) is persisted before the new answer — a failed
    or aborted edit leaves the original message and its answer untouched.

    `cacheable_system`/`anthropic_question` are only ever populated by the
    ask-stream endpoint (the one call site with a stable per-conversation
    checkpoint to isolate — see build_context_prompt_with_cache_split);
    regenerate-stream and edit-stream leave both None and get today's
    behavior unchanged. `context_free` (see run_orchestrator's docstring)
    defaults False for the same reason: only the ask-stream call site has
    verified there's no history/system-prompt behind the question.

    `remember_memory`/`memory_question`/`memory_vector` (see app/memory.py)
    are likewise only ever set by ask-stream — cross-conversation memory is
    scoped to genuinely new turns, not a regenerated or edited answer to one
    already remembered.
    """

    def event_stream() -> Iterator[str]:
        accumulated: list[str] = []
        mode_used = "unknown"
        orchestrator_stream = stream_orchestrator(
            contextual_req,
            routing_question,
            owner,
            history=history,
            cacheable_system=cacheable_system,
            anthropic_question=anthropic_question,
            context_free=context_free,
        )

        try:
            for event in orchestrator_stream:
                name = str(event["event"])
                data = dict(event["data"])

                if name == "meta":
                    mode_used = str(data.get("mode_used", mode_used))

                elif name == "delta":
                    accumulated.append(str(data.get("text", "")))

                elif name == "done":
                    answer = str(data.get("answer", ""))
                    mode_used = str(data.get("mode_used", mode_used))
                    if answer.strip():
                        data["notes"] = f"{data.get('notes', '')} | {context_note}"
                        # Replace-in-place happens here (not up front), so the old
                        # message(s) survive any earlier failure. Persisted before
                        # the terminal frame so clients can refetch on "done".
                        if edit_message_id is not None:
                            delete_messages_from(conversation_id, edit_message_id)
                            add_message(
                                conversation_id=conversation_id,
                                role="user",
                                content=edit_question or "",
                                images=_encode_images(edit_images),
                                files=_encode_files(edit_files),
                            )
                        elif replace_after_id is not None:
                            delete_messages_after(conversation_id, replace_after_id)
                        add_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=answer,
                            mode_used=mode_used,
                            notes=str(data["notes"]),
                            input_tokens=data.get("input_tokens"),
                            output_tokens=data.get("output_tokens"),
                            cost_usd=data.get("cost_usd"),
                            cached=bool(data.get("cached", False)),
                            sources=json.dumps(data["sources"])
                            if data.get("sources")
                            else None,
                            pending_action=json.dumps(data["pending_action"])
                            if data.get("pending_action")
                            else None,
                            action_status="pending"
                            if data.get("pending_action")
                            else None,
                            images=json.dumps(data["images"])
                            if data.get("images")
                            else None,
                            truncated=bool(data.get("truncated", False)),
                            code_results=json.dumps(data["code_results"])
                            if data.get("code_results")
                            else None,
                            fact_checks=json.dumps(data["fact_checks"])
                            if data.get("fact_checks")
                            else None,
                        )
                        if remember_memory:
                            memory.remember(
                                owner,
                                conversation_id,
                                memory_question or "",
                                answer,
                                memory_vector,
                            )
                    else:
                        # Empty 'done' (model returned nothing, or a reasoning call
                        # truncated before any output): keep history as-is — never
                        # blank a good prior answer on regenerate, nor write an empty
                        # bubble on ask — and tell the client nothing was saved.
                        #
                        # A truncated reasoning call can be empty yet costly. It is
                        # intentionally not stored as a message (an empty row purely
                        # to carry cost would reintroduce the pollution this guard
                        # prevents), but its cost is NOT lost: stream_orchestrator
                        # records it to the spend_log, so the daily budget still sees
                        # it. The client is also told here that nothing was saved.
                        data["notes"] = (
                            f"{data.get('notes', '')} | {context_note} "
                            "| not saved (empty answer)"
                        )

                elif name == "error":
                    # A regeneration or edit that fails keeps the existing message(s)
                    # and discards the partial; a normal ask persists whatever streamed.
                    partial = "".join(accumulated).strip()
                    if replace_after_id is None and edit_message_id is None and partial:
                        add_message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=partial,
                            mode_used=mode_used,
                            notes=(
                                f"Interrupted before completion: "
                                f"{data.get('message', '')} | {context_note}"
                            ),
                        )

                yield f"event: {name}\ndata: {json.dumps(data)}\n\n"
        except GeneratorExit:
            # The client disconnected (Stop button, tab close, network drop)
            # mid-stream — Starlette closes this generator, raising
            # GeneratorExit at the `yield` above. Deterministically close the
            # inner generator now (not left to GC) so stream_orchestrator's own
            # GeneratorExit handling runs and records whatever spend it already
            # incurred, then persist whatever text streamed so far — same
            # treatment as a provider error mid-stream (the "error" branch
            # above): never silently drop a partial answer the user was
            # already reading.
            orchestrator_stream.close()
            partial = "".join(accumulated).strip()
            if replace_after_id is None and edit_message_id is None and partial:
                add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=partial,
                    mode_used=mode_used,
                    notes=f"Interrupted before completion: client disconnected | {context_note}",
                )
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _prepare_regeneration(
    conversation_id: int, req: RegenerateRequest
) -> tuple[AskRequest, str, int, str]:
    """Build the retry request for the last user turn (without deleting anything).

    Returns (request, context_note, last_user_message_id, routing_question). The
    old answer is deleted only once the new one is ready, so a failed retry loses
    nothing. `routing_question` is the raw last user turn, used to route on the
    question rather than the assembled history. Raises 400 if the conversation
    has no user message to regenerate.
    """
    conversation = get_conversation(conversation_id)
    messages = list_messages(conversation_id)
    last_user = next(
        (m for m in reversed(messages) if m["role"] == "user"),
        None,
    )
    if last_user is None:
        raise HTTPException(
            status_code=400, detail="No user message to regenerate an answer for."
        )

    last_user_id = int(last_user["id"])
    last_user_question = str(last_user["content"])
    prior = [m for m in messages if int(m["id"]) < last_user_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=last_user_question,
        system_prompt=conversation.get("system_prompt") if conversation else None,
    )

    # Reuse whatever images/files the original turn was asked with, so a retry
    # sees the same vision/document input rather than silently losing it.
    raw_images = last_user.get("images")
    last_user_images = json.loads(str(raw_images)) if raw_images else None
    raw_files = last_user.get("files")
    last_user_files = json.loads(str(raw_files)) if raw_files else None

    raw_req = AskRequest(
        question=context_question,
        mode=req.mode,
        no_cache=True,  # a regeneration is always fresh (no cache read or write)
        model=req.model,
        images=last_user_images,
        files=last_user_files,
    )
    # Apply the conversation's model pin, same as every other ask-path (ask,
    # ask/stream, edit) — this was the one path that forgot to, so a pinned
    # conversation's regenerate silently ignored the pin and routed by
    # req.mode/req.model instead.
    contextual_req = (
        _pinned_ask_request(conversation, context_question, raw_req)
        if conversation
        else raw_req
    )
    context_note = f"regenerated | context_messages={len(prior)}"
    return contextual_req, context_note, last_user_id, last_user_question


@router.post(
    "/v1/conversations/{conversation_id}/regenerate", response_model=AskResponse
)
@limiter.limit(rate_limit_value)
def regenerate_conversation(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )

    result = run_orchestrator(
        contextual_req, routing_question=routing_question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | {context_note}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
        code_results=result.code_results,
        fact_checks=result.fact_checks,
        truncated=result.truncated,
    )

    if response.answer.strip():
        # Success: swap in the new answer. On failure, keep the existing answer.
        delete_messages_after(conversation_id, last_user_id)
        add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response.answer,
            mode_used=response.mode_used,
            notes=response.notes,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            cached=response.cached,
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
            fact_checks=_encode_fact_checks(response.fact_checks),
        )

    return response


@router.post("/v1/conversations/{conversation_id}/regenerate/stream")
@limiter.limit(rate_limit_value)
def regenerate_conversation_stream(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        replace_after_id=last_user_id,
        routing_question=routing_question,
        owner=owner,
    )


def _prepare_edit(
    conversation: dict, conversation_id: int, message_id: int, req: AskRequest
) -> tuple[AskRequest, str, str]:
    """Build the retry request for editing message_id (without deleting
    anything yet).

    Returns (request, context_note, routing_question). Context is built from
    only the messages BEFORE message_id — the edited message and everything
    after it are deleted only once the new answer is ready (see
    _stream_and_persist's edit_message_id), so a failed edit loses nothing.
    Raises 404 if the message doesn't belong to this conversation, 400 if
    it isn't a user message (only a user turn can be edited).
    """
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "user":
        raise HTTPException(status_code=400, detail="Only a user message can be edited")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=req.question,
        system_prompt=conversation.get("system_prompt"),
    )
    contextual_req = _pinned_ask_request(conversation, context_question, req)
    context_note = f"edited | context_messages={len(prior)}"
    return contextual_req, context_note, req.question


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/edit",
    response_model=AskResponse,
)
@limiter.limit(rate_limit_value)
def edit_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
    )

    result = run_orchestrator(
        contextual_req, routing_question=routing_question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | {context_note}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
        code_results=result.code_results,
        fact_checks=result.fact_checks,
        truncated=result.truncated,
    )

    if response.answer.strip():
        # Success: swap in the edited message and its new answer. On failure,
        # keep the original message and answer untouched.
        delete_messages_from(conversation_id, message_id)
        add_message(
            conversation_id=conversation_id,
            role="user",
            content=req.question,
            images=_encode_images(req.images),
            files=_encode_files(req.files),
        )
        add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response.answer,
            mode_used=response.mode_used,
            notes=response.notes,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            cached=response.cached,
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
            fact_checks=_encode_fact_checks(response.fact_checks),
        )

    return response


@router.post("/v1/conversations/{conversation_id}/messages/{message_id}/edit/stream")
@limiter.limit(rate_limit_value)
def edit_message_stream(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=routing_question,
        owner=owner,
        edit_message_id=message_id,
        edit_question=req.question,
        edit_images=req.images,
        edit_files=req.files,
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/action",
    response_model=ActionResult,
)
def resolve_action(
    conversation_id: int,
    message_id: int,
    req: ActionConfirmRequest,
    owner: str | None = Depends(current_owner),
):
    """Confirm or decline a message's proposed action (propose-then-confirm).

    Nothing is ever fired automatically by the orchestrator — this endpoint is
    the ONLY path that can trigger the webhook, and only on an explicit
    confirm=true from the caller.
    """
    _owned_or_404(conversation_id, owner)

    message = get_message(message_id)
    if message is None or int(message["conversation_id"]) != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.get("action_status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Action already resolved (status={message.get('action_status')!r}).",
        )

    if not req.confirm:
        claimed = claim_pending_action(message_id, "declined")
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="Action already resolved by a concurrent request.",
            )
        return ActionResult(action_status=str(claimed["action_status"]))

    # Claim the action atomically before firing the webhook, so two concurrent
    # confirm requests can't both pass the pending-check above and both post.
    # Only the request whose UPDATE actually matches the still-pending row
    # wins the claim; the loser gets a 409 instead of double-firing.
    claimed = claim_pending_action(message_id, "confirmed")
    if claimed is None:
        raise HTTPException(
            status_code=409, detail="Action already resolved by a concurrent request."
        )

    stored_action = json.loads(str(message["pending_action"]))
    action_name = str(stored_action.get("action", ""))
    payload = stored_action.get("payload", {})
    success, detail = post_webhook(action_name, payload)
    if not success:
        updated = set_action_status(message_id, "failed")
        assert updated is not None
        return ActionResult(action_status=str(updated["action_status"]), detail=detail)
    return ActionResult(action_status=str(claimed["action_status"]), detail=detail)
