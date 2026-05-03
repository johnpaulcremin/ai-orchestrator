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
