from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from .database import (
    add_message,
    create_conversation,
    get_conversation,
    init_db,
    list_conversations,
    list_messages,
)
from .orchestrator import run_orchestrator
from .schemas import (
    AskRequest,
    AskResponse,
    ConversationCreate,
    ConversationOut,
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


@app.get("/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut])
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

    add_message(
        conversation_id=conversation_id,
        role="user",
        content=req.question,
    )

    response = run_orchestrator(req)

    add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=response.answer,
        mode_used=response.mode_used,
        notes=response.notes,
    )

    return response