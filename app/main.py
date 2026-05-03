from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI

from .schemas import AskRequest, AskResponse
from .orchestrator import run_orchestrator

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="AI Orchestrator API",
    version="0.1.0",
)


@app.get("/")
def root():
    return {"status": "ok", "service": "ai-orchestrator"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/ask", response_model=AskResponse)
def ask(req: AskRequest):
    return run_orchestrator(req)

@app.get("/v1/status")
def status():
    return {
        "status": "ok",
        "service": "ai-orchestrator",
        "version": "0.1.0",
    }