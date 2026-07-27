from __future__ import annotations

import json
import re
from enum import Enum
from typing import Literal

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


# A generous cap, not a meaningful constraint on ordinary use (pasting a long
# document to ask about is fine) — a defensive limit against an unbounded
# body, consistent with every other free-text field in this schema module
# (e.g. imported messages at _MAX_IMPORT_MESSAGE_CHARS below).
_MAX_QUESTION_CHARS = 100_000


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_QUESTION_CHARS,
        description="User question/prompt",
    )
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
    # True when the provider stopped generating because it hit
    # max_output_tokens, not because it was actually finished — the answer is
    # genuinely incomplete, not just short. The UI offers a Continue action.
    truncated: bool = False


_MIN_COMPARE_MODELS = 2
_MAX_COMPARE_MODELS = 4


class CompareRequest(BaseModel):
    """Ask the same question of several specific models side-by-side — a
    direct, one-shot comparison tool distinct from routing (nothing here is
    persisted as a conversation)."""

    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_CHARS)
    models: list[str] = Field(
        ..., min_length=_MIN_COMPARE_MODELS, max_length=_MAX_COMPARE_MODELS
    )

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        cleaned = [validate_model_value(model) for model in value]
        if any(not model for model in cleaned):
            raise ValueError("model names must not be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("models must be distinct")
        return cleaned


class CompareResult(BaseModel):
    model: str
    answer: str
    mode_used: str
    notes: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    elapsed_ms: int


class CompareResponse(BaseModel):
    question: str
    results: list[CompareResult]


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


# A previously exported conversation is re-created from scratch (fresh ids,
# no model calls) rather than restoring the original row-for-row. Everything
# duplicate_conversation() also copies (pin, instructions, favorite status,
# and per-message tokens/cost/cached/sources) is restored here too, for the
# same reason
# Export produces it in the first place: text, numbers, and title/url pairs,
# none of it a binary blob. Attachments (images/files) are the one exception,
# deliberately NOT restored: re-validating and re-storing arbitrary base64
# blobs from an uploaded file is a meaningfully larger attack surface than
# this backup/restore convenience is worth.
_MAX_IMPORT_MESSAGES = 500
_MAX_IMPORT_MESSAGE_CHARS = 100_000
_MAX_SYSTEM_PROMPT_CHARS = 4_000


class ImportMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=_MAX_IMPORT_MESSAGE_CHARS)
    mode_used: str | None = None
    notes: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None


_MAX_TAGS = 15
_MAX_TAG_LENGTH = 30


def _normalize_tags(tags: list[str]) -> list[str]:
    """Trim, drop blanks/dupes, cap length — used by both the tags-set
    endpoint and Import, so the two accept the same shape."""
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        cleaned = tag.strip()[:_MAX_TAG_LENGTH]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


class ConversationImport(BaseModel):
    title: str = Field(default="Imported conversation", min_length=1, max_length=200)
    pinned_model: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=_MAX_SYSTEM_PROMPT_CHARS)
    favorite: bool = False
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)
    messages: list[ImportMessage] = Field(
        ..., min_length=1, max_length=_MAX_IMPORT_MESSAGES
    )

    @field_validator("pinned_model")
    @classmethod
    def _validate_pinned_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = validate_model_value(value)  # raises on a malformed name
        return cleaned or None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


class ConversationOut(BaseModel):
    id: int
    title: str
    owner: str | None = None
    pinned_model: str | None = None
    system_prompt: str | None = None
    favorite: bool = False
    archived: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    # Only populated by the list endpoint (a correlated subquery there); every
    # other conversation-returning endpoint omits it and this default stands
    # in, since none of them render a message count.
    message_count: int = 0

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, value: object) -> object:
        # SQLite stores this as a JSON string; decode before validation.
        # Malformed JSON degrades to an empty list rather than a 500.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []


class ConversationSystemPrompt(BaseModel):
    # Custom instructions (persona/style/rules) prepended to every question in
    # this conversation; empty string clears it.
    system_prompt: str = Field(default="", max_length=_MAX_SYSTEM_PROMPT_CHARS)


class ConversationFavorite(BaseModel):
    favorite: bool


class ConversationArchive(BaseModel):
    archived: bool


class ConversationTags(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


class MessageBookmark(BaseModel):
    bookmarked: bool


class SearchResult(BaseModel):
    id: int
    title: str
    owner: str | None = None
    pinned_model: str | None = None
    created_at: str
    updated_at: str
    snippet: str


class UsageByModel(BaseModel):
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None when NONE of this model's calls in the window have a known cost
    # (an unpriced model — see usage.py's estimate_cost) — distinct from a
    # genuinely free model (e.g. local Ollama), which reports 0.0.
    cost_usd: float | None


class UsageByDay(BaseModel):
    date: str
    cost_usd: float


class UsageSummary(BaseModel):
    today_usd: float
    days: int
    by_model: list[UsageByModel]
    by_day: list[UsageByDay]
    # The configured caps themselves (not live global spend — that stays
    # private to the operator; see budget.py). None when that cap isn't set.
    daily_budget_usd: float | None = None
    daily_budget_per_owner_usd: float | None = None
    # How much of the caller's OWN per-owner cap is left today, floored at 0.
    # None when no per-owner cap is configured — distinct from "$0 left".
    owner_remaining_usd: float | None = None


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
    bookmarked: bool = False
    # See AskResponse.truncated — same meaning, persisted so it survives a
    # reload instead of only being known at the moment the answer streamed in.
    truncated: bool = False
    created_at: str

    @field_validator("cached", "truncated", mode="before")
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


# Well above any reasonable chat answer, short of being a pathological upload
# — the actual OpenAI TTS input cap (4096 chars) is enforced server-side by
# truncating, not by rejecting the request (see speech.py).
_MAX_SPEECH_TEXT_CHARS = 50_000


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=_MAX_SPEECH_TEXT_CHARS)


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
