from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import cache
from .actions import post_webhook
from .auth import _bearer_token, current_owner, require_api_token
from .budget import budget_status, would_exceed
from .observability import setup_tracing
from .ratelimit import (
    auth_limiter,
    auth_rate_limit_value,
    limiter,
    rate_limit_value,
    rate_limiting_enabled,
)
from .database import (
    add_message,
    claim_pending_action,
    clear_settings,
    create_conversation,
    create_user,
    delete_conversation,
    delete_message,
    delete_messages_after,
    delete_messages_from,
    duplicate_conversation,
    delete_setting,
    get_conversation,
    get_message,
    get_user_by_username,
    init_db,
    list_conversations,
    list_messages,
    record_spend,
    search_conversations,
    set_action_status,
    set_conversation_pin,
    set_conversation_system_prompt,
    set_setting,
    update_conversation_title,
    usage_summary,
)
from .context_summary import summarize_conversation
from .orchestrator import run_orchestrator, stream_orchestrator, summarize_text
from .telemetry import elapsed_ms, new_request_meta
from .schemas import (
    ActionConfirmRequest,
    ActionResult,
    AskRequest,
    AskResponse,
    CompareRequest,
    CompareResponse,
    CompareResult,
    ConversationCreate,
    ConversationImport,
    ConversationOut,
    ConversationPin,
    ConversationSystemPrompt,
    ConversationUpdate,
    FileAttachment,
    LoginRequest,
    MessageOut,
    Mode,
    PendingAction,
    RegenerateRequest,
    RegisterRequest,
    SearchResult,
    SettingUpdate,
    SpeakRequest,
    Source,
    TokenResponse,
    TranscribeRequest,
    TranscribeResponse,
    UsageSummary,
    UserOut,
)
from .speech import SpeechError, speech_model, synthesize_speech
from .transcription import TranscriptionError, transcribe_audio, transcription_model
from .usage import estimate_speech_cost, estimate_transcription_cost
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
    revoke_token,
    revoke_user_sessions,
    subject_from_token,
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
# slowapi's handler is typed (Request, RateLimitExceeded) -> Response, narrower
# than Starlette's (Request, Exception) protocol, so mypy flags the variance.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


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


def _summarize_history_enabled() -> bool:
    raw = (os.getenv("SUMMARIZE_HISTORY") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def build_context_prompt(
    prior_messages: list[dict[str, Any]],
    current_question: str,
    system_prompt: str | None = None,
    summarize: Callable[[str], str] | None = None,
) -> str:
    clean_system_prompt = (system_prompt or "").strip()

    if not prior_messages and not clean_system_prompt:
        return current_question

    if not prior_messages:
        # No history yet, but custom instructions are set: skip the
        # conversation-history framing entirely rather than describing history
        # that doesn't exist.
        return (
            f"Instructions for this conversation:\n{clean_system_prompt}\n\n"
            f"Current user question:\n{current_question}"
        )

    recent_messages = prior_messages[-12:]
    older_messages = prior_messages[:-12]

    # Fold everything older than the recent window into a compact summary so long
    # threads keep their whole context instead of silently forgetting it. Best
    # effort: an empty summary (disabled, no older turns, or a failed call) leaves
    # the prompt byte-identical to the recent-only version.
    summary = ""
    if older_messages and _summarize_history_enabled():
        summarizer = summarize if summarize is not None else summarize_text
        summary = summarize_conversation(older_messages, summarizer)

    lines = [
        "You are continuing a saved conversation.",
        "Use the conversation history below when it is relevant.",
        "Do not claim you lack context if the answer is present in the history.",
        "",
    ]

    if clean_system_prompt:
        lines.extend(["Instructions for this conversation:", clean_system_prompt, ""])

    if summary:
        lines.extend(["Summary of earlier messages:", summary, ""])

    lines.append("Conversation history:")

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


def _encode_sources(sources: list[Source] | None) -> str | None:
    """A message's web-search citations as a JSON string for storage, or None."""
    if not sources:
        return None
    return json.dumps([s.model_dump() for s in sources])


def _encode_action(pending_action: PendingAction | None) -> str | None:
    """A message's proposed action as a JSON string for storage, or None."""
    if pending_action is None:
        return None
    return json.dumps(pending_action.model_dump())


def _encode_images(images: list[str] | None) -> str | None:
    """A message's generated images as a JSON string for storage, or None."""
    if not images:
        return None
    return json.dumps(images)


def _encode_files(files: list[FileAttachment] | None) -> str | None:
    """A message's attached documents as a JSON string for storage, or None."""
    if not files:
        return None
    return json.dumps([f.model_dump() for f in files])


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
            # "" (falsy) means the budget tier is disabled — unlike fast/smart,
            # it has no default; unset = the tier doesn't exist for the UI.
            "budget": model_setting("OPENAI_MODEL_BUDGET", ""),
            "fast": model_setting("OPENAI_MODEL_FAST", base_model),
            "smart": model_setting("OPENAI_MODEL_SMART", base_model),
            "fallback": model_setting("OPENAI_MODEL_FALLBACK", ""),
        },
        # Daily spend cap: only whether a cap is active — live spend figures are
        # withheld from this public, unauthenticated endpoint.
        "budget": budget_status(),
    }


@app.post("/v1/auth/register", response_model=UserOut, status_code=201)
@auth_limiter.limit(auth_rate_limit_value)
def register(request: Request, req: RegisterRequest):
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
@auth_limiter.limit(auth_rate_limit_value)
def login(request: Request, req: LoginRequest):
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )

    user = get_user_by_username(req.username.strip())
    if user is None or not verify_password(req.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    return TokenResponse(access_token=create_access_token(req.username.strip()))


def _require_jwt_enabled() -> None:
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )


@app.post("/v1/auth/logout")
@auth_limiter.limit(auth_rate_limit_value)
def logout(request: Request, authorization: str | None = Header(default=None)):
    """Log the user out everywhere: invalidate all of their existing tokens.

    Bumping the user's session epoch also kills any token that was refreshed onto
    a fresh jti, so a compromised session can't outlive a logout.
    """
    _require_jwt_enabled()
    token = _bearer_token(authorization)
    subject = subject_from_token(token) if token else None
    if subject is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    revoke_user_sessions(subject)
    return {"status": "logged_out"}


@app.post("/v1/auth/refresh", response_model=TokenResponse)
@auth_limiter.limit(auth_rate_limit_value)
def refresh(request: Request, authorization: str | None = Header(default=None)):
    """Trade a still-valid, non-revoked token for a fresh one, rotating it.

    The presented token is revoked, so a leaked token can't be replayed after the
    holder refreshes.
    """
    _require_jwt_enabled()
    token = _bearer_token(authorization)
    subject = subject_from_token(token) if token else None
    if subject is None:
        raise HTTPException(
            status_code=401, detail="Invalid, expired, or revoked token."
        )
    revoke_token(token)  # rotate: the old token stops working immediately
    return TokenResponse(access_token=create_access_token(subject))


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


# Pin values that mean "use this tier" rather than "force this exact model".
_TIER_PINS = {"budget", "fast", "smart"}


def _pinned_ask_request(
    conversation: dict, question: str, req: AskRequest
) -> AskRequest:
    """Apply the conversation's model pin (if any) to a new question.

    A pin fully determines routing for normal asks: a 'fast'/'smart' pin forces
    that tier; any other value forces that exact model (bypassing the router and
    cache, like switch-model) with the generous smart-tier budget — independent
    of the request's mode, which the UI disables while pinned. No pin -> the
    request's own mode (and any client-forced `model`) is used, same as `/v1/ask`.
    """
    pin = (conversation.get("pinned_model") or "").strip()
    if pin in _TIER_PINS:
        return AskRequest(
            question=question,
            mode=Mode(pin),
            no_cache=req.no_cache,
            images=req.images,
            files=req.files,
        )
    if pin:
        return AskRequest(
            question=question,
            mode=Mode.smart,
            no_cache=req.no_cache,
            model=pin,
            images=req.images,
            files=req.files,
        )
    return AskRequest(
        question=question,
        mode=req.mode,
        no_cache=req.no_cache,
        model=req.model,
        images=req.images,
        files=req.files,
    )


@router.post("/v1/ask", response_model=AskResponse)
@limiter.limit(rate_limit_value)
def ask(
    request: Request,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    return run_orchestrator(req, owner=owner)


@router.post("/v1/compare", response_model=CompareResponse)
@limiter.limit(rate_limit_value)
def compare(
    request: Request,
    req: CompareRequest,
    owner: str | None = Depends(current_owner),
):
    """Ask the same question of 2-4 specific models and report each answer
    alongside its cost/tokens/latency — a direct way to see what
    multi-provider routing actually trades off.

    Dispatched one model at a time, not in parallel: keeps the daily-budget
    check-then-spend accounting correct across the whole batch, and matches
    run_orchestrator's own guarantee — it never raises for an ordinary
    provider failure, only reports an empty answer + explanatory notes — so
    one model being unconfigured/failing never aborts the rest of the
    comparison.
    """
    results = []
    for model in req.models:
        meta = new_request_meta()
        response = run_orchestrator(
            AskRequest(question=req.question, model=model), owner=owner
        )
        results.append(
            CompareResult(
                model=model,
                answer=response.answer,
                mode_used=response.mode_used,
                notes=response.notes,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                elapsed_ms=elapsed_ms(meta),
            )
        )

    return CompareResponse(question=req.question, results=results)


@router.post("/v1/transcribe", response_model=TranscribeResponse)
@limiter.limit(rate_limit_value)
def transcribe(
    request: Request, req: TranscribeRequest, owner: str | None = Depends(current_owner)
):
    """Transcribe a recorded voice clip (mic-button dictation in the UI).

    A synchronous utility call, not part of the routing/fallback machinery —
    unlike /v1/ask, a failure here is a real HTTP error rather than a 200 with
    an empty answer, since there's no tier/fallback story to narrate through
    `notes`. It IS still subject to the daily budget cap (gated pre-dispatch,
    recorded on success), same as every other billable call.
    """
    model = transcription_model()
    cost = estimate_transcription_cost()
    refusal = would_exceed(model, 0, extra_cost_usd=cost)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        text = transcribe_audio(req.audio)
    except TranscriptionError as err:
        raise HTTPException(status_code=502, detail=str(err)) from err
    record_spend(owner, model, 0, 0, cost)
    return TranscribeResponse(text=text)


@router.post("/v1/speak")
@limiter.limit(rate_limit_value)
def speak(
    request: Request, req: SpeakRequest, owner: str | None = Depends(current_owner)
):
    """Synthesize an assistant answer to speech (speaker-button playback in
    the UI). Raw audio/mpeg bytes, not JSON — the client plays them directly.

    Same synchronous-utility trust level as /v1/transcribe: a real HTTP error
    on failure, not the /v1/ask always-200 convention, and likewise subject to
    the daily budget cap.
    """
    model = speech_model()
    cost = estimate_speech_cost(req.text)
    refusal = would_exceed(model, 0, extra_cost_usd=cost)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        audio = synthesize_speech(req.text)
    except SpeechError as err:
        raise HTTPException(status_code=502, detail=str(err)) from err
    record_spend(owner, model, 0, 0, cost)
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/v1/search", response_model=list[SearchResult])
def search(
    q: str = Query(..., min_length=1, max_length=200),
    owner: str | None = Depends(current_owner),
):
    """Search this owner's conversations by title or message content."""
    return search_conversations(owner, q)


@router.get("/v1/usage", response_model=UsageSummary)
def usage(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own spend: today's total, by-model breakdown, by-day series."""
    return usage_summary(owner, days)


@router.get("/v1/conversations", response_model=list[ConversationOut])
def conversations(owner: str | None = Depends(current_owner)):
    return list_conversations(owner)


@router.post("/v1/conversations", response_model=ConversationOut)
def new_conversation(
    req: ConversationCreate, owner: str | None = Depends(current_owner)
):
    return create_conversation(req.title, owner)


@router.post("/v1/conversations/import", response_model=ConversationOut)
def import_conversation(
    req: ConversationImport, owner: str | None = Depends(current_owner)
):
    """Re-create a conversation from a previously exported JSON file.

    Builds a fresh conversation with new message ids and no model calls —
    text only (role/content/mode_used/notes); attachments are not restored.
    """
    conversation = create_conversation(req.title, owner)
    for message in req.messages:
        add_message(
            conversation_id=conversation["id"],
            role=message.role,
            content=message.content,
            mode_used=message.mode_used,
            notes=message.notes,
        )

    return get_conversation(conversation["id"])


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


@router.put("/v1/conversations/{conversation_id}/pin", response_model=ConversationOut)
def pin_conversation_model(
    conversation_id: int,
    req: ConversationPin,
    owner: str | None = Depends(current_owner),
):
    """Pin a model (or 'fast'/'smart' tier) to a conversation; empty clears it."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_pin(conversation_id, req.model)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put(
    "/v1/conversations/{conversation_id}/system_prompt", response_model=ConversationOut
)
def set_conversation_instructions(
    conversation_id: int,
    req: ConversationSystemPrompt,
    owner: str | None = Depends(current_owner),
):
    """Set this conversation's custom instructions; empty clears them."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_system_prompt(conversation_id, req.system_prompt)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.post(
    "/v1/conversations/{conversation_id}/duplicate", response_model=ConversationOut
)
def duplicate_conversation_endpoint(
    conversation_id: int, owner: str | None = Depends(current_owner)
):
    """Copy this conversation (title, pin, instructions, every message) into
    a brand-new one owned by the caller. Any pending action is not carried
    over — see duplicate_conversation for why."""
    _owned_or_404(conversation_id, owner)
    conversation = duplicate_conversation(conversation_id, owner)
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


@router.delete("/v1/conversations/{conversation_id}/messages/{message_id}")
def remove_message(
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Delete a single message (either role) without touching any other
    message — distinct from regenerate/edit, which both replace or discard
    a range of messages and produce a fresh answer."""
    _owned_or_404(conversation_id, owner)
    deleted = delete_message(conversation_id, message_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"status": "deleted", "message_id": message_id}


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
        images=_encode_images(req.images),
        files=_encode_files(req.files),
    )

    context_question = build_context_prompt(
        prior_messages=prior_messages,
        current_question=req.question,
        system_prompt=conversation.get("system_prompt"),
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    # Route on the new user turn, not the assembled context prompt.
    result = run_orchestrator(
        contextual_req, routing_question=req.question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | context_messages={len(prior_messages)}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
    )

    # Only persist a real answer: an empty/failed reply (auth error, rate limit,
    # all fallbacks exhausted) must not write an empty assistant bubble. The user
    # turn is already saved and the failure is returned to the client in `notes`.
    if response.answer.strip():
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
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
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
        images=_encode_images(req.images),
        files=_encode_files(req.files),
    )

    context_question = build_context_prompt(
        prior_messages=prior_messages,
        current_question=req.question,
        system_prompt=conversation.get("system_prompt"),
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    context_note = f"context_messages={len(prior_messages)}"

    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=req.question,
        owner=owner,
    )


def _stream_and_persist(
    conversation_id: int,
    contextual_req: AskRequest,
    context_note: str,
    replace_after_id: int | None = None,
    routing_question: str | None = None,
    owner: str | None = None,
    edit_message_id: int | None = None,
    edit_question: str | None = None,
    edit_images: list[str] | None = None,
    edit_files: list[FileAttachment] | None = None,
) -> StreamingResponse:
    """Stream an orchestrator response as SSE and persist the assistant message.

    Shared by the ask-stream, regenerate-stream, and edit-stream endpoints.
    When `replace_after_id` is set (regenerate), the previous answer(s) after
    that message are deleted only on a successful `done` — right before the
    new answer is stored — so a failed or aborted regeneration leaves the old
    answer intact. `edit_message_id` (edit) works the same way but ALSO
    replaces the edited user message itself: on success, that message and
    everything after it is deleted and a fresh user message (`edit_question`/
    `edit_images`/`edit_files`) is persisted before the new answer — a failed
    or aborted edit leaves the original message and its answer untouched.
    """

    def event_stream() -> Iterator[str]:
        accumulated: list[str] = []
        mode_used = "unknown"

        for event in stream_orchestrator(contextual_req, routing_question, owner):
            name = str(event["event"])
            data = dict(event["data"])

            if name == "meta":
                mode_used = str(data.get("mode_used", mode_used))

            elif name == "delta":
                accumulated.append(str(data.get("text", "")))

            elif name == "done":
                answer = str(data.get("answer", ""))
                mode_used = str(data.get("mode_used", mode_used))
                if answer.strip():
                    data["notes"] = f"{data.get('notes', '')} | {context_note}"
                    # Replace-in-place happens here (not up front), so the old
                    # message(s) survive any earlier failure. Persisted before
                    # the terminal frame so clients can refetch on "done".
                    if edit_message_id is not None:
                        delete_messages_from(conversation_id, edit_message_id)
                        add_message(
                            conversation_id=conversation_id,
                            role="user",
                            content=edit_question or "",
                            images=_encode_images(edit_images),
                            files=_encode_files(edit_files),
                        )
                    elif replace_after_id is not None:
                        delete_messages_after(conversation_id, replace_after_id)
                    add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=answer,
                        mode_used=mode_used,
                        notes=str(data["notes"]),
                        input_tokens=data.get("input_tokens"),
                        output_tokens=data.get("output_tokens"),
                        cost_usd=data.get("cost_usd"),
                        cached=bool(data.get("cached", False)),
                        sources=json.dumps(data["sources"])
                        if data.get("sources")
                        else None,
                        pending_action=json.dumps(data["pending_action"])
                        if data.get("pending_action")
                        else None,
                        action_status="pending" if data.get("pending_action") else None,
                        images=json.dumps(data["images"])
                        if data.get("images")
                        else None,
                    )
                else:
                    # Empty 'done' (model returned nothing, or a reasoning call
                    # truncated before any output): keep history as-is — never
                    # blank a good prior answer on regenerate, nor write an empty
                    # bubble on ask — and tell the client nothing was saved.
                    #
                    # A truncated reasoning call can be empty yet costly. It is
                    # intentionally not stored as a message (an empty row purely
                    # to carry cost would reintroduce the pollution this guard
                    # prevents), but its cost is NOT lost: stream_orchestrator
                    # records it to the spend_log, so the daily budget still sees
                    # it. The client is also told here that nothing was saved.
                    data["notes"] = (
                        f"{data.get('notes', '')} | {context_note} "
                        "| not saved (empty answer)"
                    )

            elif name == "error":
                # A regeneration or edit that fails keeps the existing message(s)
                # and discards the partial; a normal ask persists whatever streamed.
                partial = "".join(accumulated).strip()
                if replace_after_id is None and edit_message_id is None and partial:
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


def _prepare_regeneration(
    conversation_id: int, req: RegenerateRequest
) -> tuple[AskRequest, str, int, str]:
    """Build the retry request for the last user turn (without deleting anything).

    Returns (request, context_note, last_user_message_id, routing_question). The
    old answer is deleted only once the new one is ready, so a failed retry loses
    nothing. `routing_question` is the raw last user turn, used to route on the
    question rather than the assembled history. Raises 400 if the conversation
    has no user message to regenerate.
    """
    conversation = get_conversation(conversation_id)
    messages = list_messages(conversation_id)
    last_user = next(
        (m for m in reversed(messages) if m["role"] == "user"),
        None,
    )
    if last_user is None:
        raise HTTPException(
            status_code=400, detail="No user message to regenerate an answer for."
        )

    last_user_id = int(last_user["id"])
    last_user_question = str(last_user["content"])
    prior = [m for m in messages if int(m["id"]) < last_user_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=last_user_question,
        system_prompt=conversation.get("system_prompt") if conversation else None,
    )

    # Reuse whatever images/files the original turn was asked with, so a retry
    # sees the same vision/document input rather than silently losing it.
    raw_images = last_user.get("images")
    last_user_images = json.loads(str(raw_images)) if raw_images else None
    raw_files = last_user.get("files")
    last_user_files = json.loads(str(raw_files)) if raw_files else None

    raw_req = AskRequest(
        question=context_question,
        mode=req.mode,
        no_cache=True,  # a regeneration is always fresh (no cache read or write)
        model=req.model,
        images=last_user_images,
        files=last_user_files,
    )
    # Apply the conversation's model pin, same as every other ask-path (ask,
    # ask/stream, edit) — this was the one path that forgot to, so a pinned
    # conversation's regenerate silently ignored the pin and routed by
    # req.mode/req.model instead.
    contextual_req = (
        _pinned_ask_request(conversation, context_question, raw_req)
        if conversation
        else raw_req
    )
    context_note = f"regenerated | context_messages={len(prior)}"
    return contextual_req, context_note, last_user_id, last_user_question


@router.post(
    "/v1/conversations/{conversation_id}/regenerate", response_model=AskResponse
)
@limiter.limit(rate_limit_value)
def regenerate_conversation(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )

    result = run_orchestrator(
        contextual_req, routing_question=routing_question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | {context_note}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
    )

    if response.answer.strip():
        # Success: swap in the new answer. On failure, keep the existing answer.
        delete_messages_after(conversation_id, last_user_id)
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
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
        )

    return response


@router.post("/v1/conversations/{conversation_id}/regenerate/stream")
@limiter.limit(rate_limit_value)
def regenerate_conversation_stream(
    request: Request,
    conversation_id: int,
    req: RegenerateRequest,
    owner: str | None = Depends(current_owner),
):
    _owned_or_404(conversation_id, owner)
    contextual_req, context_note, last_user_id, routing_question = (
        _prepare_regeneration(conversation_id, req)
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        replace_after_id=last_user_id,
        routing_question=routing_question,
        owner=owner,
    )


def _prepare_edit(
    conversation: dict, conversation_id: int, message_id: int, req: AskRequest
) -> tuple[AskRequest, str, str]:
    """Build the retry request for editing message_id (without deleting
    anything yet).

    Returns (request, context_note, routing_question). Context is built from
    only the messages BEFORE message_id — the edited message and everything
    after it are deleted only once the new answer is ready (see
    _stream_and_persist's edit_message_id), so a failed edit loses nothing.
    Raises 404 if the message doesn't belong to this conversation, 400 if
    it isn't a user message (only a user turn can be edited).
    """
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "user":
        raise HTTPException(status_code=400, detail="Only a user message can be edited")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=req.question,
        system_prompt=conversation.get("system_prompt"),
    )
    contextual_req = _pinned_ask_request(conversation, context_question, req)
    context_note = f"edited | context_messages={len(prior)}"
    return contextual_req, context_note, req.question


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/edit",
    response_model=AskResponse,
)
@limiter.limit(rate_limit_value)
def edit_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
    )

    result = run_orchestrator(
        contextual_req, routing_question=routing_question, owner=owner
    )

    response = AskResponse(
        answer=result.answer,
        mode_used=result.mode_used,
        notes=f"{result.notes} | {context_note}",
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        cached=result.cached,
        sources=result.sources,
        pending_action=result.pending_action,
        images=result.images,
    )

    if response.answer.strip():
        # Success: swap in the edited message and its new answer. On failure,
        # keep the original message and answer untouched.
        delete_messages_from(conversation_id, message_id)
        add_message(
            conversation_id=conversation_id,
            role="user",
            content=req.question,
            images=_encode_images(req.images),
            files=_encode_files(req.files),
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
            sources=_encode_sources(response.sources),
            pending_action=_encode_action(response.pending_action),
            action_status="pending" if response.pending_action else None,
            images=_encode_images(response.images),
        )

    return response


@router.post("/v1/conversations/{conversation_id}/messages/{message_id}/edit/stream")
@limiter.limit(rate_limit_value)
def edit_message_stream(
    request: Request,
    conversation_id: int,
    message_id: int,
    req: AskRequest,
    owner: str | None = Depends(current_owner),
):
    conversation = _owned_or_404(conversation_id, owner)
    contextual_req, context_note, routing_question = _prepare_edit(
        conversation, conversation_id, message_id, req
    )
    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=routing_question,
        owner=owner,
        edit_message_id=message_id,
        edit_question=req.question,
        edit_images=req.images,
        edit_files=req.files,
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/action",
    response_model=ActionResult,
)
def resolve_action(
    conversation_id: int,
    message_id: int,
    req: ActionConfirmRequest,
    owner: str | None = Depends(current_owner),
):
    """Confirm or decline a message's proposed action (propose-then-confirm).

    Nothing is ever fired automatically by the orchestrator — this endpoint is
    the ONLY path that can trigger the webhook, and only on an explicit
    confirm=true from the caller.
    """
    _owned_or_404(conversation_id, owner)

    message = get_message(message_id)
    if message is None or int(message["conversation_id"]) != conversation_id:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.get("action_status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Action already resolved (status={message.get('action_status')!r}).",
        )

    if not req.confirm:
        claimed = claim_pending_action(message_id, "declined")
        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="Action already resolved by a concurrent request.",
            )
        return ActionResult(action_status=str(claimed["action_status"]))

    # Claim the action atomically before firing the webhook, so two concurrent
    # confirm requests can't both pass the pending-check above and both post.
    # Only the request whose UPDATE actually matches the still-pending row
    # wins the claim; the loser gets a 409 instead of double-firing.
    claimed = claim_pending_action(message_id, "confirmed")
    if claimed is None:
        raise HTTPException(
            status_code=409, detail="Action already resolved by a concurrent request."
        )

    payload = json.loads(str(message["pending_action"])).get("payload", {})
    success, detail = post_webhook(payload)
    if not success:
        updated = set_action_status(message_id, "failed")
        assert updated is not None
        return ActionResult(action_status=str(updated["action_status"]), detail=detail)
    return ActionResult(action_status=str(claimed["action_status"]), detail=detail)


app.include_router(router)
