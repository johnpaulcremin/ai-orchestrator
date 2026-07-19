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

from . import cache
from .auth import current_owner, require_api_token
from .observability import setup_tracing
from .ratelimit import limiter, rate_limit_value, rate_limiting_enabled
from .database import (
    add_message,
    clear_settings,
    create_conversation,
    create_user,
    delete_conversation,
    delete_setting,
    get_conversation,
    get_user_by_username,
    init_db,
    list_conversations,
    list_messages,
    set_setting,
    update_conversation_title,
)
from .orchestrator import run_orchestrator, stream_orchestrator
from .schemas import (
    AskRequest,
    AskResponse,
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    LoginRequest,
    MessageOut,
    RegisterRequest,
    SettingUpdate,
    TokenResponse,
    UserOut,
)
from .settings import (
    SETTABLE_KEYS,
    describe_settings,
    model_setting,
    settings_writable,
    validate_model_value,
)
from .security import (
    create_access_token,
    hash_password,
    jwt_enabled,
    registration_allowed,
    verify_password,
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
    # Re-evaluate now that .env is loaded (the limiter was constructed at import,
    # possibly before load_dotenv ran).
    limiter.enabled = rate_limiting_enabled()
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
    # The default titles the UI and API create; a first message replaces them.
    clean_title = title.strip().lower()
    return clean_title in {
        "untitled conversation",
        "new ai workbench conversation",
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
    static_auth = bool(os.getenv("API_AUTH_TOKEN", "").strip())
    base_model = model_setting("OPENAI_MODEL", "gpt-5")
    return {
        "status": "ok",
        "service": "ai-orchestrator",
        "version": "0.1.0",
        "auth_enabled": static_auth or jwt_enabled(),
        "jwt_enabled": jwt_enabled(),
        "registration_allowed": jwt_enabled() and registration_allowed(),
        # Effective models (a saved override wins over the env var), so the UI
        # header reflects what routing will actually use.
        "models": {
            "router": model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano"),
            "fast": model_setting("OPENAI_MODEL_FAST", base_model),
            "smart": model_setting("OPENAI_MODEL_SMART", base_model),
            "fallback": model_setting("OPENAI_MODEL_FALLBACK", ""),
        },
    }


@app.post("/v1/auth/register", response_model=UserOut, status_code=201)
def register(req: RegisterRequest):
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )
    if not registration_allowed():
        raise HTTPException(status_code=403, detail="Registration is disabled.")

    user = create_user(req.username.strip(), hash_password(req.password))
    if user is None:
        raise HTTPException(status_code=409, detail="Username already exists.")

    return user


@app.post("/v1/auth/login", response_model=TokenResponse)
def login(req: LoginRequest):
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )

    user = get_user_by_username(req.username.strip())
    if user is None or not verify_password(req.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    return TokenResponse(access_token=create_access_token(req.username.strip()))


@router.get("/v1/auth/me")
def me(owner: str | None = Depends(current_owner)):
    """The current principal: the username when logged in via JWT, else null."""
    return {"username": owner}


def _require_writable_settings() -> None:
    if not settings_writable():
        raise HTTPException(
            status_code=403,
            detail="Settings editing is disabled (ALLOW_SETTINGS_WRITE=false).",
        )


def _require_settable_key(key: str) -> None:
    if key not in SETTABLE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"'{key}' is not an editable setting.",
        )


@router.get("/v1/settings")
def get_settings_view():
    """The full resolved model map (tiers + task categories) for the UI."""
    return describe_settings()


@router.put("/v1/settings/{key}")
def put_setting(key: str, req: SettingUpdate):
    """Set a model override for a key, or clear it when the value is empty."""
    _require_writable_settings()
    _require_settable_key(key)

    try:
        value = validate_model_value(req.value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if value:
        set_setting(key, value)
    else:
        delete_setting(key)

    return describe_settings()


@router.delete("/v1/settings/{key}")
def clear_setting(key: str):
    """Clear a single override, reverting the key to its env var / default."""
    _require_writable_settings()
    _require_settable_key(key)
    delete_setting(key)
    return describe_settings()


@router.post("/v1/settings/reset")
def reset_settings():
    """Clear every override, reverting the whole map to env vars / defaults."""
    _require_writable_settings()
    clear_settings()
    return describe_settings()


@router.get("/v1/cache")
def cache_info():
    """Response-cache status: enabled, entry count, TTL, and size cap."""
    return cache.stats()


@router.delete("/v1/cache")
def clear_cache():
    """Empty the response cache so subsequent prompts hit the model again."""
    return {"cleared": cache.clear(), **cache.stats()}


def _owned_or_404(conversation_id: int, owner: str | None) -> dict:
    """Fetch a conversation, 404-ing if it does not exist or is not the caller's."""
    conversation = get_conversation(conversation_id)
    if conversation is None or conversation["owner"] != owner:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.post("/v1/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask(request: Request, req: AskRequest):
    return run_orchestrator(req)


@router.get("/v1/conversations", response_model=list[ConversationOut])
def conversations(owner: str | None = Depends(current_owner)):
    return list_conversations(owner)


@router.post("/v1/conversations", response_model=ConversationOut)
def new_conversation(
    req: ConversationCreate, owner: str | None = Depends(current_owner)
):
    return create_conversation(req.title, owner)


@router.patch("/v1/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: int,
    req: ConversationUpdate,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    conversation = update_conversation_title(conversation_id, req.title)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.delete("/v1/conversations/{conversation_id}")
def remove_conversation(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    _owned_or_404(conversation_id, owner)
    deleted = delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {"status": "deleted", "conversation_id": conversation_id}


@router.get(
    "/v1/conversations/{conversation_id}/messages", response_model=list[MessageOut]
)
def conversation_messages(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    _owned_or_404(conversation_id, owner)
    return list_messages(conversation_id)


@router.post("/v1/conversations/{conversation_id}/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask_conversation(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

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
        no_cache=req.no_cache,
    )

    result = run_orchestrator(contextual_req)

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | context_messages={len(prior_messages)}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
    )

    add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=response.answer,
        mode_used=response.mode_used,
        notes=response.notes,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=response.cost_usd,
        cached=response.cached,
    )

    return response


@router.post("/v1/conversations/{conversation_id}/ask/stream")
@limiter.limit(rate_limit_value)
def ask_conversation_stream(
    request: Request,
    conversation_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)

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
        no_cache=req.no_cache,
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
                    input_tokens=data.get("input_tokens"),
                    output_tokens=data.get("output_tokens"),
                    cost_usd=data.get("cost_usd"),
                    cached=bool(data.get("cached", False)),
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
