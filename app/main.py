from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

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
from .orchestrator import run_orchestrator
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

app = FastAPI(
    title="AI Orchestrator API",
    version="0.1.0",
)


@app.on_event("startup")
def startup() -> None:
    init_db()


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
    }


@app.post("/v1/ask", response_model=AskResponse)
def ask(req: AskRequest):
    return run_orchestrator(req)


@app.get("/v1/conversations", response_model=list[ConversationOut])
def conversations():
    return list_conversations()


@app.post("/v1/conversations", response_model=ConversationOut)
def new_conversation(req: ConversationCreate):
    return create_conversation(req.title)


@app.patch("/v1/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(conversation_id: int, req: ConversationUpdate):
    conversation = update_conversation_title(conversation_id, req.title)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@app.delete("/v1/conversations/{conversation_id}")
def remove_conversation(conversation_id: int):
    deleted = delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"status": "deleted", "conversation_id": conversation_id}


@app.get(
    "/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
def conversation_messages(conversation_id: int):
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return list_messages(conversation_id)


@app.post("/v1/conversations/{conversation_id}/ask", response_model=AskResponse)
def ask_conversation(conversation_id: int, req: AskRequest):
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
