from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .auth import require_api_token
from .observability import setup_tracing
from .ratelimit import limiter, rate_limit_value
from .database import (
    add_message,
    create_conversation,
    delete_conversation,
    get_conversation,
    init_db,
    list_conversations,
    list_messages,
    update_conversation_title,
)
from .orchestrator import run_orchestrator, stream_orchestrator
from .schemas import (
    AskRequest,
    AskResponse,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    setup_tracing(app)
    yield


app = FastAPI(
    title="AI Orchestrator API",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting (opt-in via RATE_LIMIT). Registered even when disabled so the
# decorators on the ask endpoints resolve; the limiter no-ops when disabled.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _allowed_origins() -> list[str]:
    raw = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter(dependencies=[Depends(require_api_token)])


def build_context_prompt(
    prior_messages: list[dict[str, Any]],
    current_question: str,
) -> str:
    if not prior_messages:
        return current_question

    recent_messages = prior_messages[-12:]

    lines = [
        "You are continuing a saved conversation.",
        "Use the conversation history below when it is relevant.",
        "Do not claim you lack context if the answer is present in the history.",
        "",
        "Conversation history:",
    ]

    for message in recent_messages:
        role = str(message.get("role", "unknown")).strip()
        content = str(message.get("content", "")).strip()

        if not content:
            continue

        lines.append(f"{role.upper()}: {content}")

    lines.extend(
        [
            "",
            "Current user question:",
            current_question,
        ]
    )

    return "\n".join(lines)


def _is_generic_title(title: str) -> bool:
    clean_title = title.strip().lower()
    return clean_title in {
        "untitled conversation",
        "new ai workbench conversation",
        "new ai workbench conversa",
        "first saved conversation",
    }


def _title_from_question(question: str) -> str:
    clean_question = " ".join(question.strip().split())

    if not clean_question:
        return "Untitled conversation"

    max_len = 70
    if len(clean_question) <= max_len:
        return clean_question

    return f"{clean_question[:max_len].rstrip()}..."


@app.get("/")
def root():
    return {"status": "ok", "service": "ai-orchestrator"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/status")
def status():
    return {
        "status": "ok",
        "service": "ai-orchestrator",
        "version": "0.1.0",
        "auth_enabled": bool(os.getenv("API_AUTH_TOKEN", "").strip()),
        "models": {
            "router": os.getenv("OPENAI_MODEL_ROUTER", ""),
            "fast": os.getenv("OPENAI_MODEL_FAST", ""),
            "smart": os.getenv("OPENAI_MODEL_SMART", ""),
            "fallback": os.getenv("OPENAI_MODEL_FALLBACK", ""),
        },
    }


@router.post("/v1/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask(request: Request, req: AskRequest):
    return run_orchestrator(req)


@router.get("/v1/conversations", response_model=list[ConversationOut])
def conversations():
    return list_conversations()


@router.post("/v1/conversations", response_model=ConversationOut)
def new_conversation(req: ConversationCreate):
    return create_conversation(req.title)


@router.patch("/v1/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(conversation_id: int, req: ConversationUpdate):
    conversation = update_conversation_title(conversation_id, req.title)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.delete("/v1/conversations/{conversation_id}")
def remove_conversation(conversation_id: int):
    deleted = delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"status": "deleted", "conversation_id": conversation_id}


@router.get(
    "/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
def conversation_messages(conversation_id: int):
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return list_messages(conversation_id)


@router.post("/v1/conversations/{conversation_id}/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask_conversation(request: Request, conversation_id: int, req: AskRequest):
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    prior_messages = list_messages(conversation_id)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
    )

    context_question = build_context_prompt(
        prior_messages=prior_messages,
        current_question=req.question,
    )

    contextual_req = AskRequest(
        question=context_question,
        mode=req.mode,
    )

    response = run_orchestrator(contextual_req)

    response = AskResponse(
        answer=response.answer,
        mode_used=response.mode_used,
        notes=f"{response.notes} | context_messages={len(prior_messages)}",
    )

    add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=response.answer,
        mode_used=response.mode_used,
        notes=response.notes,
    )

    return response


@router.post("/v1/conversations/{conversation_id}/ask/stream")
@limiter.limit(rate_limit_value)
def ask_conversation_stream(request: Request, conversation_id: int, req: AskRequest):
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    prior_messages = list_messages(conversation_id)

    if not prior_messages and _is_generic_title(str(conversation["title"])):
        update_conversation_title(
            conversation_id=conversation_id,
            title=_title_from_question(req.question),
        )

    add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
    )

    context_question = build_context_prompt(
        prior_messages=prior_messages,
        current_question=req.question,
    )

    contextual_req = AskRequest(
        question=context_question,
        mode=req.mode,
    )

    context_note = f"context_messages={len(prior_messages)}"

    def event_stream() -> Iterator[str]:
        accumulated: list[str] = []
        mode_used = "unknown"

        for event in stream_orchestrator(contextual_req):
            name = str(event["event"])
            data = dict(event["data"])

            if name == "meta":
                mode_used = str(data.get("mode_used", mode_used))

            elif name == "delta":
                accumulated.append(str(data.get("text", "")))

            elif name == "done":
                data["notes"] = f"{data.get('notes', '')} | {context_note}"
                mode_used = str(data.get("mode_used", mode_used))
                # Persist the assistant message before the terminal frame so
                # clients can refetch on "done".
                add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=str(data.get("answer", "")),
                    mode_used=mode_used,
                    notes=str(data["notes"]),
                )

            elif name == "error":
                partial = "".join(accumulated).strip()
                if partial:
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=partial,
                        mode_used=mode_used,
                        notes=(
                            f"Interrupted before completion: "
                            f"{data.get('message', '')} | {context_note}"
                        ),
                    )

            yield f"event: {name}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.include_router(router)
