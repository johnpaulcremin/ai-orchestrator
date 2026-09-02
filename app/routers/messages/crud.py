"""Message-level CRUD: cancel, bookmarks list, list-in-conversation, delete,
restore, bookmark toggle, and 👍/👎 feedback. See app/routers/messages/
_shared.py for the dedup/streaming engine shared by the ask/regenerate/edit
route families, which this module doesn't need.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ...auth import current_owner
from ...database import (
    add_message,
    conversation_spend,
    delete_message,
    get_message,
    list_bookmarked_messages,
    list_messages,
    set_message_bookmarked,
    set_message_feedback,
)
from ... import request_registry
from ...schemas import (
    BookmarkedMessage,
    ConversationSpend,
    MessageBookmark,
    MessageFeedback,
    MessageOut,
    MessageRestoreRequest,
)
from ..deps import (
    _encode_academic_results,
    _encode_audio,
    _encode_code_results,
    _encode_fact_checks,
    _encode_files,
    _encode_images,
    _encode_videos,
    _encode_library_sources,
    _encode_math_results,
    _encode_memory_sources,
    _encode_sources,
    _encode_workflow_steps,
    _owned_or_404,
    router,
)


@router.post("/v1/requests/{request_id}/cancel")
def cancel_request(request_id: str):
    """Explicit abort — the Stop button's cancellation signal, distinct
    from a bare client disconnect (see app/request_registry.py's module
    docstring and _stream_and_persist's for the full rationale). Marks the
    matching in-flight worker's entry so it stops itself at its next
    check — between provider-stream events for a streaming ask/regenerate/
    edit, or not at all for a non-streaming call (see _dedup_or_call; a
    non-streaming ask has no natural checkpoint to abort at mid-call, so
    this only ever affects the streaming endpoints in practice).

    No owner check: `request_id` is an unguessable, single-use, short-lived
    UUID the client itself generated and is the only party who could ever
    know it — the same trust boundary a share-link token already relies on
    (see app/routers/shares.py), not a resource needing per-owner ACLs.
    Returns `{"cancelled": bool}` — False for an unknown or already-finished
    request_id, never an error (cancelling something that's already done is
    a no-op, not a mistake worth failing loudly over).
    """
    return {"cancelled": request_registry.mark_aborted(request_id)}


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


@router.get(
    "/v1/conversations/{conversation_id}/spend", response_model=ConversationSpend
)
def conversation_spend_total(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    """What this conversation actually cost, from the spend log.

    Separate from the messages endpoint deliberately: a client that only wants
    to render the transcript should not pay for a spend aggregate, and the two
    change at different times (a message is edited or deleted; spend never is
    — it is an accounting record). See schemas.ConversationSpend for why the
    message-derived total is not the whole story.
    """
    _owned_or_404(conversation_id, owner)
    totals = conversation_spend(conversation_id)
    attributed = sum(
        float(m["cost_usd"] or 0.0) for m in list_messages(conversation_id)
    )
    return ConversationSpend(
        cost_usd=float(totals["cost_usd"]),
        input_tokens=int(totals["input_tokens"]),
        output_tokens=int(totals["output_tokens"]),
        # Never negative: a cached hit costs the conversation nothing but is
        # persisted with the original call's cost_usd for display, so the
        # message-derived sum can legitimately exceed logged spend.
        unattributed_cost_usd=max(0.0, float(totals["cost_usd"]) - attributed),
    )


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
        max_output_tokens=req.max_output_tokens,
        code_results=_encode_code_results(req.code_results),
        fact_checks=_encode_fact_checks(req.fact_checks),
        academic_results=_encode_academic_results(req.academic_results),
        math_results=_encode_math_results(req.math_results),
        library_sources=_encode_library_sources(req.library_sources),
        memory_sources=_encode_memory_sources(req.memory_sources),
        workflow_steps=_encode_workflow_steps(req.workflow_steps),
        images=_encode_images(req.images),
        videos=_encode_videos(req.videos),
        files=_encode_files(req.files),
        audio=_encode_audio(req.audio),
        model=req.model,
        feedback=req.feedback,
        feedback_reason=req.feedback_reason,
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


_FEEDBACK_VERDICTS = {"up": 1, "down": -1}


@router.put(
    "/v1/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageOut,
)
def rate_message(
    conversation_id: int,
    message_id: int,
    req: MessageFeedback,
    owner: str | None = Depends(current_owner),
):
    """Rate/clear a caller's 👍/👎 on one assistant answer — the quality
    signal that closes the loop on this app's cost-only routing metrics
    (see app/feedback.py). Assistant messages only: rating a user's own
    turn makes no sense, so that's a 422, not a silent no-op. Setting the
    SAME verdict already recorded clears it instead (see
    database.set_message_feedback) — the click-again-to-clear contract the
    UI relies on, enforced here so a direct API call gets it too."""
    _owned_or_404(conversation_id, owner)

    message = get_message(message_id)
    if message is None or int(message["conversation_id"]) != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message["role"] != "assistant":
        raise HTTPException(
            status_code=422, detail="Only assistant messages can be rated."
        )

    verdict = _FEEDBACK_VERDICTS.get(req.verdict) if req.verdict else None
    updated = set_message_feedback(
        conversation_id, message_id, owner, verdict, req.reason
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated
