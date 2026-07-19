from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Mode(str, Enum):
    auto = "auto"
    fast = "fast"
    smart = "smart"


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question/prompt")
    mode: Mode = Field(default=Mode.auto, description="Routing mode")
    no_cache: bool = Field(
        default=False,
        description="Bypass the response cache read (force a fresh answer)",
    )


class AskResponse(BaseModel):
    answer: str
    mode_used: str
    notes: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False


class ConversationCreate(BaseModel):
    title: str = Field(default="Untitled conversation", min_length=1)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1)


class ConversationOut(BaseModel):
    id: int
    title: str
    owner: str | None = None
    created_at: str
    updated_at: str


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
    created_at: str

    @field_validator("cached", mode="before")
    @classmethod
    def _coerce_cached(cls, value: object) -> bool:
        # SQLite stores this as 0/1/NULL; normalise to a bool for the API.
        return bool(value)


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
