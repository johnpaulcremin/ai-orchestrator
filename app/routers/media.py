"""Voice input/output: /v1/transcribe (mic-button dictation) and /v1/speak
(assistant-message playback). Both are discrete, synchronous utility calls —
not part of the ask/routing/fallback machinery — but still gated by the
daily budget cap like any other billable call.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import Depends, HTTPException, Query, Request, Response

from .. import budget
from ..auth import current_owner
from ..database import finalize_spend, record_spend
from ..ratelimit import limiter, rate_limit_value
from ..schemas import (
    SpeakRequest,
    SpeechCostEstimate,
    SpreadsheetPreviewRequest,
    SpreadsheetPreviewResponse,
    TranscribeRequest,
    TranscribeResponse,
)
from ..spreadsheet_ingestion import csv_preview_rows, xlsx_preview_rows
from ..speech import SpeechError, speech_model, synthesize_speech
from ..transcription import TranscriptionError, transcribe_audio, transcription_model
from ..usage import (
    estimate_speech_cost,
    estimate_speech_cost_for_chars,
    estimate_transcription_cost,
)
from .deps import router

# A generous but real ceiling on the RAW (decoded) bytes this endpoint will
# parse — a preview is a cheap glance at a file's shape, not a reason to
# accept an arbitrarily large upload; a file past this just degrades to the
# plain download link (see this module's docstring on that contract).
_MAX_PREVIEW_RAW_BYTES = 10 * 1024 * 1024  # 10MB, matching CodeFile's own cap
# Mirrors CodeFile's two previewable mime types (see schemas.py's
# _CODE_FILE_MIME_ALLOWLIST) — a local copy of each data-URI prefix rather
# than importing spreadsheet_ingestion's own (module-private, xlsx-only)
# constant, since this endpoint needs the .csv one too.
_XLSX_DATA_URL_PREFIX = (
    "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,"
)
_CSV_DATA_URL_PREFIX = "data:text/csv;base64,"


@router.post("/v1/transcribe", response_model=TranscribeResponse)
@limiter.limit(rate_limit_value)
def transcribe(
    request: Request, req: TranscribeRequest, owner: str | None = Depends(current_owner)
):
    """Transcribe a recorded voice clip (mic-button dictation in the UI).

    A synchronous utility call, not part of the routing/fallback machinery —
    unlike /v1/ask, a failure here is a real HTTP error rather than a 200 with
    an empty answer, since there's no tier/fallback story to narrate through
    `notes`. It IS still subject to the daily budget cap (gated pre-dispatch,
    recorded on success), same as every other billable call.
    """
    model = transcription_model()
    cost = estimate_transcription_cost()
    refusal, reservation_id = budget.reserve(model, 0, extra_cost_usd=cost, owner=owner)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        text = transcribe_audio(req.audio)
    except TranscriptionError as err:
        budget.release(reservation_id)
        raise HTTPException(status_code=502, detail=str(err)) from err
    if reservation_id is not None:
        finalize_spend(reservation_id, 0, 0, cost)
    else:
        record_spend(owner, model, 0, 0, cost)
    return TranscribeResponse(text=text)


@router.get("/v1/speak/cost", response_model=SpeechCostEstimate)
def speak_cost(chars: int = Query(ge=0, le=100_000)):
    """What a /v1/speak call of `chars` characters would cost, before making
    it.

    Exists because the paid AI voice and the browser's own free voice sit
    behind the same speaker button, distinguished only by a dropdown that
    said "$ AI" and a hover tooltip — so on a touch screen the first tap of a
    fresh session spent money with nothing shown beforehand. The UI now
    quotes this figure and asks, once per session, before the first paid clip.

    Takes a character count rather than the text: the estimate only depends
    on length (see usage.estimate_speech_cost_for_chars), and an answer the
    user has not decided to synthesize yet has no business being POSTed
    anywhere. Deliberately unauthenticated-cheap — it reads no owner data,
    touches no provider, and returns the same number for everyone.
    """
    return SpeechCostEstimate(
        chars=chars,
        estimated_cost_usd=estimate_speech_cost_for_chars(chars),
        model=speech_model(),
    )


@router.post("/v1/speak")
@limiter.limit(rate_limit_value)
def speak(
    request: Request, req: SpeakRequest, owner: str | None = Depends(current_owner)
):
    """Synthesize an assistant answer to speech (speaker-button playback in
    the UI). Raw audio/mpeg bytes, not JSON — the client plays them directly.

    Same synchronous-utility trust level as /v1/transcribe: a real HTTP error
    on failure, not the /v1/ask always-200 convention, and likewise subject to
    the daily budget cap.
    """
    model = speech_model()
    cost = estimate_speech_cost(req.text)
    refusal, reservation_id = budget.reserve(model, 0, extra_cost_usd=cost, owner=owner)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        audio = synthesize_speech(req.text)
    except SpeechError as err:
        budget.release(reservation_id)
        raise HTTPException(status_code=502, detail=str(err)) from err
    if reservation_id is not None:
        finalize_spend(reservation_id, 0, 0, cost)
    else:
        record_spend(owner, model, 0, 0, cost)
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/v1/spreadsheet-preview", response_model=SpreadsheetPreviewResponse)
@limiter.limit(rate_limit_value)
def spreadsheet_preview(
    request: Request,
    req: SpreadsheetPreviewRequest,
    owner: str | None = Depends(current_owner),
):
    """Parse a generated .xlsx/.csv file's data URI into a small inline
    preview grid (see app/spreadsheet_ingestion.py's xlsx_preview_rows/
    csv_preview_rows) — the inline-preview counterpart to the plain download
    link a code_results file already offers. Free, local, no LLM/budget
    involved (unlike /v1/transcribe and /v1/speak above): this is CPU-only
    parsing of a file the caller already has.

    Always a real HTTP error on anything that isn't a clean parse (bad
    base64, unsupported mime, oversized payload, corrupt/malformed file) —
    the frontend's contract is to degrade to the existing plain download
    link on any non-200, never to show a broken preview or fail the whole
    message.
    """
    if req.data.startswith(_XLSX_DATA_URL_PREFIX):
        prefix_len = len(_XLSX_DATA_URL_PREFIX)
        parser = xlsx_preview_rows
    elif req.data.startswith(_CSV_DATA_URL_PREFIX):
        prefix_len = len(_CSV_DATA_URL_PREFIX)
        parser = csv_preview_rows
    else:
        raise HTTPException(
            status_code=422, detail="Only .xlsx/.csv files can be previewed."
        )

    try:
        raw = base64.b64decode(req.data[prefix_len:], validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail="Invalid base64 data") from err

    if len(raw) > _MAX_PREVIEW_RAW_BYTES:
        raise HTTPException(status_code=422, detail="File is too large to preview")

    try:
        preview = parser(raw)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err

    rows = preview.rows
    truncated = len(rows) < preview.total_rows or (
        bool(rows) and len(rows[0]) < preview.total_cols
    )
    return SpreadsheetPreviewResponse(
        rows=rows,
        total_rows=preview.total_rows,
        total_cols=preview.total_cols,
        truncated=truncated,
        sheet_name=preview.sheet_name,
    )
