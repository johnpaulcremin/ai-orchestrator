from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from .settings import validate_model_value


def _clean_forced_model(value: str | None) -> str | None:
    """Validate an optional forced-model name; '' / whitespace -> None."""
    if value is None:
        return None
    cleaned = validate_model_value(value)  # raises ValueError on a malformed name
    return cleaned or None


class Mode(str, Enum):
    auto = "auto"
    fast = "fast"
    smart = "smart"
    budget = "budget"


# Vision input attachments: at most this many per message, and each capped in
# size (base64 chars, ~9MB raw at the 4/3 encoding overhead) — a defensive
# limit against a client sending a pathologically large payload, not a
# meaningful constraint on ordinary photos/screenshots.
_MAX_INPUT_IMAGES = 4
_MAX_INPUT_IMAGE_CHARS = 12_000_000
# Only a data: URL is accepted (never a bare http(s) URL): passing an arbitrary
# remote URL through as `image_url` would have the PROVIDER's servers fetch it
# on our behalf — an SSRF vector via a third party. Restricting to base64 data
# closes that off at the schema layer.
_DATA_IMAGE_URL_RE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/]+=*$"
)


# Document attachments (PDF or plain text): at most this many per message,
# each capped in size (base64 chars, ~15MB raw at the 4/3 encoding overhead).
# Deliberately a small, exact-match mime allowlist (not "any file") — these two
# are the only types the provider-side document blocks (see providers.py) know
# how to handle; a bare http(s) URL is rejected for the same SSRF reason as
# images (see _DATA_IMAGE_URL_RE).
_MAX_INPUT_FILES = 4
_MAX_INPUT_FILE_CHARS = 20_000_000
_DATA_FILE_URL_RE = re.compile(
    r"^data:(application/pdf|text/plain);base64,[A-Za-z0-9+/]+=*$"
)


class FileAttachment(BaseModel):
    """A document (PDF or plain text) the user attached for the model to read."""

    filename: str = Field(..., min_length=1, max_length=200)
    data: str = Field(..., description="data:{application/pdf,text/plain};base64,...")

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("filename must not be empty")
        return cleaned

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        if len(value) > _MAX_INPUT_FILE_CHARS:
            raise ValueError("attached file is too large")
        if not _DATA_FILE_URL_RE.match(value):
            raise ValueError(
                "files must be data:{application/pdf,text/plain};base64,... URLs"
            )
        return value


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question/prompt")
    mode: Mode = Field(default=Mode.auto, description="Routing mode")
    no_cache: bool = Field(
        default=False,
        description="Bypass the response cache entirely — no read and no write",
    )
    model: str | None = Field(
        default=None,
        description="Force this exact model, bypassing routing (also skips cache)",
    )
    images: list[str] | None = Field(
        default=None,
        description=(
            "Attached vision input, as data:image/...;base64,... URLs "
            f"(max {_MAX_INPUT_IMAGES})"
        ),
    )
    files: list[FileAttachment] | None = Field(
        default=None,
        description=f"Attached documents (PDF or plain text), max {_MAX_INPUT_FILES}",
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        return _clean_forced_model(value)

    @field_validator("images")
    @classmethod
    def _validate_images(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        if len(value) > _MAX_INPUT_IMAGES:
            raise ValueError(f"at most {_MAX_INPUT_IMAGES} images per message")
        for url in value:
            if len(url) > _MAX_INPUT_IMAGE_CHARS:
                raise ValueError("attached image is too large")
            if not _DATA_IMAGE_URL_RE.match(url):
                raise ValueError(
                    "images must be data:image/{png,jpeg,gif,webp};base64,... URLs"
                )
        return value

    @field_validator("files")
    @classmethod
    def _validate_files(
        cls, value: list[FileAttachment] | None
    ) -> list[FileAttachment] | None:
        if not value:
            return None
        if len(value) > _MAX_INPUT_FILES:
            raise ValueError(f"at most {_MAX_INPUT_FILES} files per message")
        return value


class Source(BaseModel):
    """A web citation the model's answer relied on (web_search retrieval)."""

    title: str
    url: str


class PendingAction(BaseModel):
    """A real-world action the model proposed (send email, update a sheet, ...).

    Propose-then-confirm: this is only ever a PROPOSAL. Nothing fires until the
    caller explicitly confirms it via POST .../messages/{id}/action — the model
    can suggest an action but never execute one unilaterally.
    """

    action: str
    summary: str
    payload: dict[str, object]


class AskResponse(BaseModel):
    answer: str
    mode_used: str
    notes: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None
    pending_action: PendingAction | None = None
    # Generated images (image_generation tool), as ready-to-render
    # `data:image/png;base64,...` URLs.
    images: list[str] | None = None


class RegenerateRequest(BaseModel):
    """Re-run the conversation's last user question (always fresh, no cache)."""

    mode: Mode = Field(default=Mode.auto, description="Routing mode for the retry")
    model: str | None = Field(
        default=None, description="Force this exact model for the regeneration"
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        return _clean_forced_model(value)


class ConversationCreate(BaseModel):
    title: str = Field(default="Untitled conversation", min_length=1)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1)


class ConversationOut(BaseModel):
    id: int
    title: str
    owner: str | None = None
    pinned_model: str | None = None
    created_at: str
    updated_at: str


class ConversationPin(BaseModel):
    # A model name (forced) or 'fast'/'smart' tier; empty string clears the pin.
    model: str = Field(default="", max_length=200)

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        return validate_model_value(value)  # raises on a malformed name; '' stays ''


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    mode_used: str | None = None
    notes: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None
    pending_action: PendingAction | None = None
    # "pending" | "confirmed" | "declined" | "failed"; None when there was never
    # a proposed action on this message.
    action_status: str | None = None
    # For an assistant message: images the model generated. For a user
    # message: images the user attached (vision input). Same shape either way
    # (data:image/...;base64,... URLs); `role` disambiguates the meaning.
    images: list[str] | None = None
    # Documents (PDF/plain text) the user attached; always None on assistant
    # messages — the model can read a file, never produce one.
    files: list[FileAttachment] | None = None
    created_at: str

    @field_validator("cached", mode="before")
    @classmethod
    def _coerce_cached(cls, value: object) -> bool:
        # SQLite stores this as 0/1/NULL; normalise to a bool for the API.
        return bool(value)

    @field_validator("sources", "pending_action", "images", "files", mode="before")
    @classmethod
    def _parse_json_column(cls, value: object) -> object:
        # SQLite stores these as a JSON string (or NULL); decode before pydantic
        # validates them. Malformed JSON degrades to None rather than a 500 — a
        # display nicety, not worth failing the request.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None


class ActionConfirmRequest(BaseModel):
    """Body for POST .../messages/{id}/action. confirm=false just declines
    (records the outcome, never touches the webhook)."""

    confirm: bool


class ActionResult(BaseModel):
    action_status: str
    detail: str | None = None


# Recorded voice clips: capped well above a typical few-minutes dictation clip
# but short of OpenAI's ~25MB file limit, to reject a pathological upload
# before it reaches the provider.
_MAX_AUDIO_CHARS = 34_000_000
_DATA_AUDIO_URL_RE = re.compile(
    r"^data:audio/(webm|wav|mp3|mpeg|mp4|m4a|ogg);base64,[A-Za-z0-9+/]+=*$"
)


class TranscribeRequest(BaseModel):
    audio: str = Field(
        ..., description="A recorded voice clip as data:audio/...;base64,..."
    )

    @field_validator("audio")
    @classmethod
    def _validate_audio(cls, value: str) -> str:
        if len(value) > _MAX_AUDIO_CHARS:
            raise ValueError("audio clip is too large")
        if not _DATA_AUDIO_URL_RE.match(value):
            raise ValueError(
                "audio must be a data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,... URL"
            )
        return value


class TranscribeResponse(BaseModel):
    text: str


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def _trimmed_username(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 3:
            raise ValueError("username must be at least 3 characters after trimming")
        return trimmed


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    created_at: str


class SettingUpdate(BaseModel):
    # An empty value clears the override (reverts the key to its env/default).
    value: str = Field(default="", max_length=200)
