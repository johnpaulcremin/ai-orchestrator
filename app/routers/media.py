"""Voice input/output: /v1/transcribe (mic-button dictation) and /v1/speak
(assistant-message playback). Both are discrete, synchronous utility calls —
not part of the ask/routing/fallback machinery — but still gated by the
daily budget cap like any other billable call.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response

from .. import budget
from ..auth import current_owner
from ..database import finalize_spend, record_spend
from ..ratelimit import limiter, rate_limit_value
from ..schemas import SpeakRequest, TranscribeRequest, TranscribeResponse
from ..speech import SpeechError, speech_model, synthesize_speech
from ..transcription import TranscriptionError, transcribe_audio, transcription_model
from ..usage import estimate_speech_cost, estimate_transcription_cost
from .deps import router


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
