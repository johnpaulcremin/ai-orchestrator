"""Saved prompt templates — reusable composer snippets, distinct from a
single conversation's own Custom Instructions.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ..auth import current_owner
from ..database import create_template, delete_template, list_templates, update_template
from ..schemas import TemplateCreate, TemplateOut, TemplateUpdate
from .deps import _owned_template_or_404, router


@router.get("/v1/templates", response_model=list[TemplateOut])
def templates(owner: str | None = Depends(current_owner)):
    """This owner's saved prompt templates, most-recently-updated first —
    reusable snippets insertable into any conversation's composer, distinct
    from a single conversation's own Custom Instructions."""
    return list_templates(owner)


@router.post("/v1/templates", response_model=TemplateOut, status_code=201)
def create_template_endpoint(
    req: TemplateCreate, owner: str | None = Depends(current_owner)
):
    return create_template(owner, req.name, req.content)


@router.patch("/v1/templates/{template_id}", response_model=TemplateOut)
def update_template_endpoint(
    template_id: int,
    req: TemplateUpdate,
    owner: str | None = Depends(current_owner),
):
    _owned_template_or_404(template_id, owner)
    if req.name is None and req.content is None:
        raise HTTPException(
            status_code=400, detail="Provide a name and/or content to update"
        )
    updated = update_template(template_id, name=req.name, content=req.content)
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@router.delete("/v1/templates/{template_id}")
def delete_template_endpoint(
    template_id: int, owner: str | None = Depends(current_owner)
):
    _owned_template_or_404(template_id, owner)
    delete_template(template_id)
    return {"status": "deleted", "template_id": template_id}
