"""The RAG document library: upload/list/delete reference documents the
model can automatically draw on (see app/rag_library.py for the
chunk/embed/retrieve machinery this feeds). Distinct from a per-message
FileAttachment, which the upload endpoint's own request body reuses
verbatim for identical mime/size validation.
"""

from __future__ import annotations

import base64
import json

from fastapi import Depends, HTTPException

from .. import budget, rag_library
from ..auth import current_owner
from ..database import (
    finalize_spend,
    library_chunk_add,
    library_document_create,
    library_document_delete,
    library_document_set_chunk_count,
    library_documents_list,
    record_spend,
)
from ..schemas import FileAttachment, LibraryDocument
from ..semantic_cache import _embedding_model
from ..usage import estimate_embedding_cost
from .deps import router


def _ingest_chunks(
    owner: str | None, filename: str, mime_type: str, size: int, chunks: list[str]
) -> dict:
    """Shared embed/store/budget sequence behind both
    upload_library_document and seed_app_docs: reserve estimated embedding
    cost up front, embed+store each chunk, finalize spend, and roll back
    (delete the just-created document) if every embedding call failed —
    same reserve-then-finalize pattern as /v1/transcribe and /v1/speak in
    app/routers/media.py.
    """
    embedding_model = _embedding_model()
    total_cost = sum(estimate_embedding_cost(chunk) for chunk in chunks)
    refusal, reservation_id = budget.reserve(
        embedding_model, 0, extra_cost_usd=total_cost, owner=owner
    )
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)

    document = library_document_create(owner, filename, mime_type, size)
    stored = 0
    for index, chunk in enumerate(chunks):
        vector = rag_library.embed(chunk)
        if vector is None:
            continue
        library_chunk_add(document["id"], owner, index, chunk, json.dumps(vector))
        stored += 1
    library_document_set_chunk_count(document["id"], stored)

    if reservation_id is not None:
        finalize_spend(reservation_id, 0, 0, total_cost)
    else:
        record_spend(owner, embedding_model, 0, 0, total_cost)

    if stored == 0:
        # Every chunk's embedding call failed (e.g. no OPENAI_API_KEY) --
        # don't leave an unusable, permanently-empty document behind.
        library_document_delete(document["id"], owner)
        raise HTTPException(
            status_code=502,
            detail="Failed to embed this document — check the embedding provider's API key.",
        )

    document["chunk_count"] = stored
    return document


@router.get("/v1/library/documents", response_model=list[LibraryDocument])
def list_library_documents(owner: str | None = Depends(current_owner)):
    """This owner's uploaded RAG library documents, most-recent first."""
    return library_documents_list(owner)


@router.post("/v1/library/documents", response_model=LibraryDocument, status_code=201)
def upload_library_document(
    req: FileAttachment, owner: str | None = Depends(current_owner)
):
    """Extract text, chunk it, embed each chunk, and store the document plus
    its chunks. `req` reuses FileAttachment's own mime allowlist (PDF/plain
    text) and size cap (see schemas.py) — the same scrutiny a per-message
    attachment gets, not a looser one just because this persists longer.

    Embedding cost is estimated per chunk and reserved against the daily
    budget cap up front (see _ingest_chunks) — a large document with many
    chunks can genuinely add up, and this is the one place in the app that
    calls embed() a variable, potentially-large number of times per request.
    """
    mime_type = req.data.split(";")[0].removeprefix("data:")
    data = base64.b64decode(req.data.split(",", 1)[1])

    text = rag_library.extract_text(mime_type, data)
    chunks = rag_library.chunk_text(text)
    if not chunks:
        raise HTTPException(
            status_code=422,
            detail="No extractable text found in this document.",
        )

    return _ingest_chunks(owner, req.filename, mime_type, len(data), chunks)


@router.post(
    "/v1/library/seed-app-docs",
    response_model=list[LibraryDocument],
    status_code=201,
)
def seed_app_docs(owner: str | None = Depends(current_owner)):
    """Ingest this app's own docs/*.md into the caller's library, so a
    conceptual "how does routing work?" style question retrieves the real
    documentation via the normal library-recall path (see
    context_builder._library_block) instead of only self_describe's terse
    JSON snapshot (see self_describe.py's module docstring for why the two
    are complementary, not redundant).

    Idempotent per filename: a doc already present in this owner's library
    (by filename — same identity FileAttachment uploads use) is skipped,
    so clicking "Seed library with app docs" again after a fresh upload
    only re-embeds and re-charges for docs that are actually new or
    changed-and-reuploaded-elsewhere-first, not the whole set every time.
    Returns the newly-created documents (empty list if nothing was new, or
    if docs/*.md isn't present — e.g. an installed package without the
    repo's docs/ folder alongside it — never an error either way).
    """
    existing_filenames = {doc["filename"] for doc in library_documents_list(owner)}
    created: list[dict] = []
    for filename, text in rag_library.app_doc_files():
        if filename in existing_filenames:
            continue
        chunks = rag_library.chunk_text(text)
        if not chunks:
            continue
        created.append(
            _ingest_chunks(owner, filename, "text/markdown", len(text), chunks)
        )
    return created


@router.delete("/v1/library/documents/{document_id}")
def delete_library_document(
    document_id: int, owner: str | None = Depends(current_owner)
):
    deleted = library_document_delete(document_id, owner)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted", "document_id": document_id}
