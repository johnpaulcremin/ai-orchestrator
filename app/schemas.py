from __future__ import annotations

import json
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

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        return _clean_forced_model(value)


class Source(BaseModel):
    """A web citation the model's answer relied on (web_search retrieval)."""

    title: str
    url: str


class AskResponse(BaseModel):
    answer: str
    mode_used: str
    notes: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None


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
    created_at: str

    @field_validator("cached", mode="before")
    @classmethod
    def _coerce_cached(cls, value: object) -> bool:
        # SQLite stores this as 0/1/NULL; normalise to a bool for the API.
        return bool(value)

    @field_validator("sources", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        # SQLite stores this as a JSON string (or NULL); decode before pydantic
        # validates it into list[Source]. Malformed JSON degrades to no sources
        # rather than a 500 — a display nicety, not worth failing the request.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None


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
