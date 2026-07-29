"""Read-only conversation share links: generate/revoke a token that lets
anyone with the link view a snapshot of a conversation without an account or
API token — for sending a conversation to someone who isn't a user of this
app at all.

Two trust boundaries, deliberately different:
  * POST/GET/DELETE .../share are owner-gated like every other conversation
    endpoint (on `router`, behind the static-token/JWT auth dependency).
  * GET /v1/shared/{token} is genuinely public (on `public_router`, no auth
    dependency at all) — the whole point is that the recipient has neither.
    Rate-limited via the always-on `auth_limiter` (not the opt-in `limiter`
    ask endpoints use), the same brute-force-resistance login/register get,
    since an unauthenticated, guessable-token-guessing vector shouldn't
    depend on an operator having set RATE_LIMIT.

At most one live token per conversation (see share_tokens' table comment in
database.init_db) — generating a new link invalidates any previous one
rather than letting old links accumulate silently.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request

from ..auth import current_owner
from ..database import (
    create_share_token,
    delete_share_tokens,
    get_conversation,
    get_conversation_id_by_token,
    get_share_token,
    list_messages,
)
from ..ratelimit import auth_limiter, auth_rate_limit_value
from ..schemas import ShareCreate, ShareStatus, SharedConversationOut
from .deps import _owned_or_404, public_router, router


def _status_from_row(row: dict[str, Any] | None) -> ShareStatus:
    if row is None:
        return ShareStatus(active=False)
    expires_at = row["expires_at"]
    return ShareStatus(
        active=True,
        token=str(row["token"]),
        expires_at=str(expires_at) if expires_at is not None else None,
    )


@router.get("/v1/conversations/{conversation_id}/share", response_model=ShareStatus)
def share_status(conversation_id: int, owner: str | None = Depends(current_owner)):
    """Whether this conversation currently has a live share link, without
    creating or changing anything."""
    _owned_or_404(conversation_id, owner)
    return _status_from_row(get_share_token(conversation_id))


@router.post("/v1/conversations/{conversation_id}/share", response_model=ShareStatus)
def create_share(
    conversation_id: int,
    req: ShareCreate,
    owner: str | None = Depends(current_owner),
):
    """(Re-)generate this conversation's share link. Any previously issued
    link for this conversation stops working immediately — see
    create_share_token."""
    _owned_or_404(conversation_id, owner)
    token = secrets.token_urlsafe(24)
    expires_at = None
    if req.ttl_hours:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=req.ttl_hours)
        ).strftime("%Y-%m-%d %H:%M:%S")
    create_share_token(conversation_id, token, expires_at)
    return _status_from_row(get_share_token(conversation_id))


@router.delete("/v1/conversations/{conversation_id}/share", response_model=ShareStatus)
def revoke_share(conversation_id: int, owner: str | None = Depends(current_owner)):
    """Revoke this conversation's share link, if any. A no-op (not an error)
    when there wasn't one — same idempotent-delete convention as
    remove_conversation."""
    _owned_or_404(conversation_id, owner)
    delete_share_tokens(conversation_id)
    return ShareStatus(active=False)


@public_router.get("/v1/shared/{token}", response_model=SharedConversationOut)
@auth_limiter.limit(auth_rate_limit_value)
def view_shared_conversation(request: Request, token: str):
    """The public, unauthenticated view a share link resolves to. 404s for an
    unknown OR expired token — identical response either way, so an expired
    link can't be distinguished from one that never existed."""
    conversation_id = get_conversation_id_by_token(token)
    if conversation_id is None:
        raise HTTPException(
            status_code=404, detail="This share link is invalid or has expired."
        )

    conversation = get_conversation(conversation_id)
    if conversation is None:
        # delete_conversation already cascades to share_tokens, so this
        # shouldn't be reachable in practice — a defensive fallback in case a
        # token ever outlives its conversation, same "gone" outcome either way.
        raise HTTPException(
            status_code=404, detail="This share link is invalid or has expired."
        )

    # A plain dict, not a constructed SharedConversationOut: `messages` here is
    # still raw DB rows (JSON-string columns included) — response_model does
    # that coercion/validation at the FastAPI boundary, same as every other
    # route in this app that returns a dict shaped like its response_model
    # rather than an already-validated instance.
    return {
        "title": str(conversation["title"]),
        "created_at": str(conversation["created_at"]),
        "messages": list_messages(conversation_id),
    }
