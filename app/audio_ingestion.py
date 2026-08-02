"""Meeting/audio ingestion: an attached audio clip is transcribed server-side
and folded into the SAME document-attachment path (schemas.FileAttachment) a
PDF or plain-text file already goes through — see providers.py's
`_anthropic_document_block` and orchestrator_extract's OpenAI equivalent,
neither of which need any change, since audio never reaches them as audio.

Deliberately NOT persisting the audio bytes: once transcribed, the transcript
is the thing worth keeping (it's what the model actually reads, and what a
regenerate/reload needs), and the raw clip is redundant with it. Only two
things survive past the request that attached it: the transcript itself (as
an ordinary FileAttachment, so it round-trips through every existing
attachment path — persistence, duplicate, branch, import/export, regenerate,
continue) and a small {filename, duration_seconds} record purely for the
UI's audio chip (schemas.AudioMeta / messages.audio).

v1 scope: only the two endpoints that accept a genuinely NEW attachment
(POST .../ask and .../ask/stream) resolve audio. Regenerate/continue re-run
against an already-persisted message and never see `audio` again — the
transcript is already sitting in that message's `files`, so nothing here
runs a second time (see tests/test_audio_ingestion.py's no-second-call
assertion). Edit does not accept new audio either, the same v1 scope
decision as chunking a too-large clip: kept out to bound this feature's
surface area rather than threading it through every attachment-accepting
endpoint at once.
"""

from __future__ import annotations

import base64

from fastapi import HTTPException

from . import budget
from .database import finalize_spend, record_spend
from .schemas import _MAX_INPUT_FILES, AudioAttachment, AudioMeta, FileAttachment
from .transcription import TranscriptionError, transcribe_audio, transcription_model
from .usage import estimate_transcription_cost


def _transcript_file(filename: str, transcript: str) -> FileAttachment:
    text = f"Transcribed from {filename}:\n\n{transcript}".strip()
    data = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return FileAttachment(
        filename=f"{filename} (transcript)",
        data=f"data:text/plain;base64,{data}",
    )


def resolve_audio_attachments(
    audio: list[AudioAttachment] | None,
    existing_files: list[FileAttachment] | None,
    owner: str | None,
) -> tuple[list[FileAttachment] | None, list[AudioMeta] | None]:
    """Transcribe every attached audio clip — billed and budget-gated
    exactly like `POST /v1/transcribe` — and fold each transcript into
    `existing_files` as a new FileAttachment, appended after whatever real
    documents were already attached. Returns `(merged_files, audio_meta)`;
    both are returned unchanged (`existing_files, None`) when `audio` is
    empty, so a caller can call this unconditionally without a branch for
    the common no-audio case.

    Raises `HTTPException(422)` if the combined attachment count would
    exceed `_MAX_INPUT_FILES`, `HTTPException(402)` on a budget refusal, or
    `HTTPException(502)` if transcription itself fails — the same status
    codes `POST /v1/transcribe` already uses for the equivalent failures.
    Each clip is billed independently: a failure on the second of two clips
    still leaves the first clip's spend recorded and its transcript in
    `files` (nothing is rolled back), the same "no cross-call transaction"
    behavior every other budget-gated call in this app has.
    """
    if not audio:
        return existing_files, None

    files = list(existing_files or [])
    if len(files) + len(audio) > _MAX_INPUT_FILES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"at most {_MAX_INPUT_FILES} attachments per message "
                "(documents + transcribed audio combined)"
            ),
        )

    model = transcription_model()
    cost = estimate_transcription_cost()
    audio_meta: list[AudioMeta] = []
    for clip in audio:
        refusal, reservation_id = budget.reserve(
            model, 0, extra_cost_usd=cost, owner=owner
        )
        if refusal is not None:
            raise HTTPException(status_code=402, detail=refusal)
        try:
            transcript = transcribe_audio(clip.data)
        except TranscriptionError as err:
            budget.release(reservation_id)
            raise HTTPException(status_code=502, detail=str(err)) from err
        if reservation_id is not None:
            finalize_spend(reservation_id, 0, 0, cost)
        else:
            record_spend(owner, model, 0, 0, cost)
        files.append(_transcript_file(clip.filename, transcript))
        audio_meta.append(
            AudioMeta(filename=clip.filename, duration_seconds=clip.duration_seconds)
        )

    return files, audio_meta
