"""Confirm/decline a message's proposed action (propose-then-confirm). Named
`action_resolution`, not `actions`, to avoid colliding with app/actions.py
(post_webhook's home module). See app/routers/messages/__init__.py's module
docstring for why `post_webhook` is read via a qualified `_messages.<name>`
reference rather than a bare imported name.
"""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException

import app.routers.messages as _messages
from ...auth import current_owner
from ...database import claim_pending_action, get_message, set_action_status
from ...schemas import ActionConfirmRequest, ActionResult
from ..deps import _owned_or_404, router


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
    success, detail = _messages.post_webhook(action_name, payload)
    if not success:
        updated = set_action_status(message_id, "failed")
        assert updated is not None
        return ActionResult(action_status=str(updated["action_status"]), detail=detail)
    return ActionResult(action_status=str(claimed["action_status"]), detail=detail)
