"""Shared router objects and cross-domain helpers for app/routers/*.

Two router instances, both attached to the FastAPI app exactly once (in
app/main.py) after every domain module below has registered its routes on
them — the split into separate files never changes which routes require the
static-token/JWT guard, only where their code physically lives.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_api_token
from ..database import get_conversation, get_template
from ..schemas import CodeResult, FactCheck, FileAttachment, PendingAction, Source

# Every /v1 route except the handful on `public_router` (auth endpoints you
# must be able to call without a token yet, plus the unauthenticated health/
# status probes) requires the static bearer token or a valid JWT.
router = APIRouter(dependencies=[Depends(require_api_token)])
public_router = APIRouter()


def _owned_or_404(conversation_id: int, owner: str | None) -> dict:
    """Fetch a conversation, 404-ing if it does not exist or is not the caller's."""
    conversation = get_conversation(conversation_id)
    if conversation is None or conversation["owner"] != owner:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _owned_template_or_404(template_id: int, owner: str | None) -> dict:
    template = get_template(template_id)
    if template is None or template["owner"] != owner:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


def _encode_sources(sources: list[Source] | None) -> str | None:
    """A message's web-search citations as a JSON string for storage, or None."""
    if not sources:
        return None
    return json.dumps([s.model_dump() for s in sources])


def _encode_action(pending_action: PendingAction | None) -> str | None:
    """A message's proposed action as a JSON string for storage, or None."""
    if pending_action is None:
        return None
    return json.dumps(pending_action.model_dump())


def _encode_images(images: list[str] | None) -> str | None:
    """A message's generated images as a JSON string for storage, or None."""
    if not images:
        return None
    return json.dumps(images)


def _encode_files(files: list[FileAttachment] | None) -> str | None:
    """A message's attached documents as a JSON string for storage, or None."""
    if not files:
        return None
    return json.dumps([f.model_dump() for f in files])


def _encode_code_results(code_results: list[CodeResult] | None) -> str | None:
    """A message's code_interpreter tool calls as a JSON string, or None."""
    if not code_results:
        return None
    return json.dumps([c.model_dump() for c in code_results])


def _encode_fact_checks(fact_checks: list[FactCheck] | None) -> str | None:
    """A message's surfaced fact-checks as a JSON string, or None."""
    if not fact_checks:
        return None
    return json.dumps([f.model_dump() for f in fact_checks])
