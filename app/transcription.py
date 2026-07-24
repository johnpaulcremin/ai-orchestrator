"""Voice input: transcribe a recorded audio clip to text via the OpenAI
transcription API, for a mic-button dictation flow in the UI.

Unlike the other "extra" features this session, this is deliberately NOT
threaded through the multi-provider orchestrator: transcription is a discrete,
explicitly user-triggered action (click record, speak, click stop) rather
than something the model or the chat flow decides to do, and this app's
OPENAI_API_KEY is already required for baseline functionality, so there is no
separate opt-in flag here — same trust level as the ask endpoint itself.
"""

from __future__ import annotations

import base64
import binascii
import os

from .orchestrator import get_client
from .telemetry import logger

# Whisper/gpt-4o-transcribe accept these container formats; kept in sync with
# the frontend's MediaRecorder mimeType choice and TranscribeRequest's
# validation (see schemas.py).
_SUPPORTED_AUDIO_MIMES = {
    "audio/webm": "webm",
    "audio/wav": "wav",
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
    "audio/ogg": "ogg",
}


def transcription_model() -> str:
    return (os.getenv("TRANSCRIPTION_MODEL") or "").strip() or "gpt-4o-mini-transcribe"


class TranscriptionError(Exception):
    """A transcription request failed — malformed input or a provider error.

    Unlike the best-effort "enrichment" helpers elsewhere in this app,
    transcription IS the entire point of the call, so failures are raised
    rather than silently degrading to empty text.
    """


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Split a `data:<mime>;base64,<data>` URL into (raw_bytes, file_extension).

    Raises TranscriptionError for anything malformed or an unsupported mime —
    callers turn this into a clean 4xx rather than a raw provider error.
    """
    if not data_url.startswith("data:") or ";base64," not in data_url:
        raise TranscriptionError("audio must be a data:audio/...;base64,... URL")
    header, b64 = data_url.split(",", 1)
    mime = header[len("data:") :].split(";", 1)[0].strip().lower()
    extension = _SUPPORTED_AUDIO_MIMES.get(mime)
    if extension is None:
        raise TranscriptionError(f"unsupported audio type: {mime or 'unknown'}")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise TranscriptionError("audio data is not valid base64") from err
    if not raw:
        raise TranscriptionError("audio data is empty")
    return raw, extension


def transcribe_audio(data_url: str) -> str:
    """Transcribe a recorded clip (data:audio/...;base64,...) to text.

    Raises TranscriptionError on any failure (malformed input, no API key,
    provider error) — callers are expected to turn that into an HTTP error
    response.
    """
    raw, extension = _decode_data_url(data_url)

    try:
        client = get_client()
    except RuntimeError as err:
        raise TranscriptionError(str(err)) from err

    try:
        result = client.audio.transcriptions.create(
            file=(f"clip.{extension}", raw, f"audio/{extension}"),
            model=transcription_model(),
        )
    except Exception as err:
        logger.exception("transcription.request_failed model=%s", transcription_model())
        raise TranscriptionError(
            f"Transcription failed: {type(err).__name__}: {err}"
        ) from err

    return (getattr(result, "text", "") or "").strip()
