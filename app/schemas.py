from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Mode(str, Enum):
    auto = "auto"
    fast = "fast"
    smart = "smart"


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="User question/prompt")
    mode: Mode = Field(default=Mode.auto, description="Routing mode")


class AskResponse(BaseModel):
    answer: str
    mode_used: str
    notes: str


class ConversationCreate(BaseModel):
    title: str = Field(default="Untitled conversation", min_length=1)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1)


class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    mode_used: str | None = None
    notes: str | None = None
    created_at: str
