"""Conversation-level CRUD and metadata: list/create/import, rename, pin,
instructions, favorite, archive, tags, duplicate, branch, summarize, delete,
and search. See app/routers/messages.py for message-level and ask/regenerate/
edit/continue endpoints nested under a conversation.
"""

from __future__ import annotations

from fastapi import BackgroundTasks, Depends, HTTPException, Query

from .. import db_backup, retention, self_report
from ..auth import current_owner
from ..database import (
    add_message,
    branch_conversation,
    create_conversation,
    delete_conversation,
    duplicate_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    search_conversations,
    set_conversation_archived,
    set_conversation_favorite,
    set_conversation_pin,
    set_conversation_system_prompt,
    set_conversation_tags,
    update_conversation_title,
)
from ..orchestrator import summarize_conversation_for_display
from ..schemas import (
    ConversationArchive,
    ConversationCreate,
    ConversationFavorite,
    ConversationImport,
    ConversationOut,
    ConversationPin,
    ConversationSystemPrompt,
    ConversationTags,
    ConversationUpdate,
    SearchResult,
)
from .deps import (
    _encode_academic_results,
    _encode_code_results,
    _encode_fact_checks,
    _encode_audio,
    _encode_files,
    _encode_images,
    _encode_videos,
    _encode_library_sources,
    _encode_math_results,
    _encode_memory_sources,
    _encode_search_queries,
    _encode_sources,
    _encode_workflow_steps,
    _owned_or_404,
    router,
)


@router.get("/v1/search", response_model=list[SearchResult])
def search(
    q: str = Query(..., min_length=1, max_length=200),
    owner: str | None = Depends(current_owner),
):
    """Search this owner's conversations by title or message content."""
    return search_conversations(owner, q)


@router.get("/v1/conversations", response_model=list[ConversationOut])
def conversations(
    background_tasks: BackgroundTasks,
    include_archived: bool = False,
    owner: str | None = Depends(current_owner),
):
    # Cheap staleness check on a route hit every time the sidebar loads — see
    # app/db_backup.py's module docstring for why this, not a background
    # scheduler, is this app's "on a schedule" for a periodic DB backup.
    backup_path = db_backup.backup_if_due()
    # Chained onto the backup call site, not a separate schedule of its
    # own — see app/retention.py's module docstring. Gated on its own
    # weekly staleness check inside maintenance_if_due, so this is still a
    # no-op on every hit except the rare one where both a backup just ran
    # AND a week has passed since the last maintenance pass.
    retention.maintenance_if_due(backup_just_ran=backup_path is not None)
    # Same "checked on sidebar load" staleness pattern, but per-owner and
    # NOT chained onto the backup call site (it has nothing to do with
    # backups) — run via BackgroundTasks so generating a due report never
    # adds latency to the sidebar load that happened to trigger it. See
    # app/self_report.py's module docstring.
    background_tasks.add_task(self_report.generate_if_due, owner)
    return list_conversations(owner, include_archived)


@router.post("/v1/conversations", response_model=ConversationOut)
def new_conversation(
    req: ConversationCreate, owner: str | None = Depends(current_owner)
):
    return create_conversation(req.title, owner)


@router.post("/v1/conversations/import", response_model=ConversationOut)
def import_conversation(
    req: ConversationImport, owner: str | None = Depends(current_owner)
):
    """Re-create a conversation from a previously exported JSON file.

    Builds a fresh conversation with new message ids and no model calls.
    Restores everything duplicate_conversation() also copies — pin,
    instructions, and per-message tokens/cost/cached/sources/truncated/
    code_results/fact_checks/academic_results/math_results/library_sources/
    memory_sources/workflow_steps/images/files/model/feedback/feedback_reason — since
    attachments now round-trip too (see ImportMessage's validators: the
    same count/size/mime checks a freshly attached upload goes through).
    """
    conversation_id = int(create_conversation(req.title, owner)["id"])
    if req.pinned_model:
        set_conversation_pin(conversation_id, req.pinned_model)
    if req.system_prompt:
        set_conversation_system_prompt(conversation_id, req.system_prompt)
    if req.favorite:
        set_conversation_favorite(conversation_id, True)
    if req.tags:
        set_conversation_tags(conversation_id, req.tags)
    for message in req.messages:
        add_message(
            conversation_id=conversation_id,
            role=message.role,
            content=message.content,
            mode_used=message.mode_used,
            notes=message.notes,
            input_tokens=message.input_tokens,
            output_tokens=message.output_tokens,
            cost_usd=message.cost_usd,
            cached=message.cached,
            sources=_encode_sources(message.sources),
            search_queries=_encode_search_queries(message.search_queries),
            truncated=message.truncated,
            max_output_tokens=message.max_output_tokens,
            code_results=_encode_code_results(message.code_results),
            fact_checks=_encode_fact_checks(message.fact_checks),
            academic_results=_encode_academic_results(message.academic_results),
            math_results=_encode_math_results(message.math_results),
            library_sources=_encode_library_sources(message.library_sources),
            memory_sources=_encode_memory_sources(message.memory_sources),
            workflow_steps=_encode_workflow_steps(message.workflow_steps),
            images=_encode_images(message.images),
            videos=_encode_videos(message.videos),
            files=_encode_files(message.files),
            audio=_encode_audio(message.audio),
            model=message.model,
            feedback=message.feedback,
            feedback_reason=message.feedback_reason,
        )

    return get_conversation(conversation_id)


@router.patch("/v1/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: int,
    req: ConversationUpdate,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    conversation = update_conversation_title(conversation_id, req.title)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put("/v1/conversations/{conversation_id}/pin", response_model=ConversationOut)
def pin_conversation_model(
    conversation_id: int,
    req: ConversationPin,
    owner: str | None = Depends(current_owner),
):
    """Pin a model (or 'fast'/'smart' tier) to a conversation; empty clears it."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_pin(conversation_id, req.model)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put(
    "/v1/conversations/{conversation_id}/system_prompt", response_model=ConversationOut
)
def set_conversation_instructions(
    conversation_id: int,
    req: ConversationSystemPrompt,
    owner: str | None = Depends(current_owner),
):
    """Set this conversation's custom instructions; empty clears them."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_system_prompt(conversation_id, req.system_prompt)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put(
    "/v1/conversations/{conversation_id}/favorite", response_model=ConversationOut
)
def favorite_conversation(
    conversation_id: int,
    req: ConversationFavorite,
    owner: str | None = Depends(current_owner),
):
    """Star (or unstar) a conversation, pinning it to the top of the sidebar."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_favorite(conversation_id, req.favorite)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put(
    "/v1/conversations/{conversation_id}/archive", response_model=ConversationOut
)
def archive_conversation(
    conversation_id: int,
    req: ConversationArchive,
    owner: str | None = Depends(current_owner),
):
    """Archive (or restore) a conversation, hiding it from the default
    sidebar list without deleting anything."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_archived(conversation_id, req.archived)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put("/v1/conversations/{conversation_id}/tags", response_model=ConversationOut)
def tag_conversation(
    conversation_id: int,
    req: ConversationTags,
    owner: str | None = Depends(current_owner),
):
    """Replace a conversation's freeform tags wholesale."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_tags(conversation_id, req.tags)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.post(
    "/v1/conversations/{conversation_id}/duplicate", response_model=ConversationOut
)
def duplicate_conversation_endpoint(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    """Copy this conversation (title, pin, instructions, every message) into
    a brand-new one owned by the caller. Any pending action is not carried
    over — see duplicate_conversation for why."""
    _owned_or_404(conversation_id, owner)
    conversation = duplicate_conversation(conversation_id, owner)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/branch",
    response_model=ConversationOut,
)
def branch_conversation_endpoint(
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Branch a new conversation from this one, copying only the messages up
    to and including `message_id` — for exploring an alternate reply to an
    earlier point without disturbing the original conversation."""
    _owned_or_404(conversation_id, owner)
    conversation = branch_conversation(conversation_id, owner, message_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation or message not found")

    return conversation


@router.post("/v1/conversations/{conversation_id}/summarize")
def summarize_conversation_endpoint(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    """A short, on-demand TL;DR of the whole conversation — key topics,
    decisions, and open questions. Ephemeral: not persisted anywhere, so
    re-clicking regenerates it fresh (and costs another cheap model call)
    rather than serving a stale cached one."""
    _owned_or_404(conversation_id, owner)
    messages = list_messages(conversation_id)
    if not messages:
        raise HTTPException(
            status_code=400, detail="Conversation has no messages to summarize."
        )
    summary = summarize_conversation_for_display(messages)
    if not summary:
        raise HTTPException(status_code=502, detail="Summarization failed.")
    return {"summary": summary}


@router.delete("/v1/conversations/{conversation_id}")
def remove_conversation(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    _owned_or_404(conversation_id, owner)
    deleted = delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"status": "deleted", "conversation_id": conversation_id}
