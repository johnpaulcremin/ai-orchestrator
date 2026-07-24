"""Voice output: synthesize speech from assistant text via OpenAI's TTS API,
for a speaker-button playback flow in the UI.

Same trust model as transcription.py: a discrete, explicitly user-triggered
action (click the speaker icon on a finished answer) rather than something
the chat flow or model decides to do, so — like transcription — this is not
threaded through the multi-provider orchestrator and needs no separate
opt-in flag beyond the OPENAI_API_KEY this app already requires.
"""

from __future__ import annotations

import os

from .orchestrator import get_client
from .telemetry import logger

# OpenAI's TTS input cap is 4096 characters; leave a small margin.
_MAX_TTS_INPUT_CHARS = 4000


def speech_model() -> str:
    return (os.getenv("SPEECH_MODEL") or "").strip() or "gpt-4o-mini-tts"


def speech_voice() -> str:
    return (os.getenv("SPEECH_VOICE") or "").strip() or "alloy"


class SpeechError(Exception):
    """Speech synthesis failed — empty input or a provider error."""


def synthesize_speech(text: str) -> bytes:
    """Synthesize `text` to MP3 bytes. Raises SpeechError on any failure.

    A long answer is truncated to _MAX_TTS_INPUT_CHARS rather than rejected —
    partial audio for a long answer is more useful than none.
    """
    clean = (text or "").strip()
    if not clean:
        raise SpeechError("text must not be empty")

    try:
        client = get_client()
    except RuntimeError as err:
        raise SpeechError(str(err)) from err

    try:
        response = client.audio.speech.create(
            input=clean[:_MAX_TTS_INPUT_CHARS],
            model=speech_model(),
            voice=speech_voice(),
            response_format="mp3",
        )
    except Exception as err:
        logger.exception("speech.request_failed model=%s", speech_model())
        raise SpeechError(
            f"Speech synthesis failed: {type(err).__name__}: {err}"
        ) from err

    return response.content
