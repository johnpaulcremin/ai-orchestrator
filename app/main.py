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
from . import budget
from .budget import budget_status, daily_budget_per_owner_usd, daily_budget_usd
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
    append_to_message,
    claim_pending_action,
    clear_settings,
    create_conversation,
    create_template,
    create_user,
    delete_conversation,
    delete_message,
    delete_messages_after,
    delete_messages_from,
    delete_template,
    branch_conversation,
    duplicate_conversation,
    delete_setting,
    finalize_spend,
    get_conversation,
    get_message,
    get_summary_cache,
    get_template,
    get_user_by_username,
    init_db,
    list_bookmarked_messages,
    list_conversations,
    list_messages,
    list_templates,
    record_spend,
    search_conversations,
    set_action_status,
    set_conversation_archived,
    set_conversation_favorite,
    set_conversation_pin,
    set_conversation_system_prompt,
    set_conversation_tags,
    set_message_bookmarked,
    set_setting,
    set_summary_cache,
    update_conversation_title,
    update_template,
    usage_summary,
)
from .context_summary import summarize_conversation
from .orchestrator import run_orchestrator, stream_orchestrator, summarize_text
from .telemetry import elapsed_ms, logger, new_request_meta
from .schemas import (
    ActionConfirmRequest,
    ActionResult,
    AskRequest,
    AskResponse,
    BookmarkedMessage,
    CodeResult,
    CompareRequest,
    CompareResponse,
    CompareResult,
    ConversationArchive,
    ConversationCreate,
    ConversationFavorite,
    ConversationImport,
    ConversationOut,
    ConversationPin,
    ConversationSystemPrompt,
    ConversationTags,
    ConversationUpdate,
    FileAttachment,
    LoginRequest,
    MessageBookmark,
    MessageOut,
    MessageRestoreRequest,
    Mode,
    PendingAction,
    RegenerateRequest,
    RegisterRequest,
    SearchResult,
    SettingUpdate,
    SpeakRequest,
    Source,
    TemplateCreate,
    TemplateOut,
    TemplateUpdate,
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
    FEATURE_FLAG_KEYS,
    SETTABLE_KEYS,
    describe_settings,
    model_setting,
    settings_writable,
    validate_bool_value,
    validate_model_value,
)
from .security import (
    admin_usernames,
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


def _warn_if_wide_open() -> None:
    """Log one loud, consolidated warning for each safety net left off.

    All of these default to "off" for a frictionless localhost dev run, which
    is the right default for that case — but the exact same defaults, copied
    straight into a Docker/internet-facing deployment (docker-compose.yml
    binds 0.0.0.0), leave the API unauthenticated, unrated, and uncapped. This
    can't stop that deployment, but it makes sure the operator can't miss it.
    """
    off = []
    if not os.getenv("API_AUTH_TOKEN", "").strip() and not jwt_enabled():
        off.append("no auth (API_AUTH_TOKEN and JWT_SECRET are both unset)")
    if not rate_limiting_enabled():
        off.append("no rate limit on ask endpoints (RATE_LIMIT is unset)")
    if daily_budget_usd() is None and daily_budget_per_owner_usd() is None:
        off.append(
            "no daily spend cap (DAILY_BUDGET_USD and DAILY_BUDGET_PER_OWNER_USD are both unset)"
        )
    if not off:
        return
    logger.warning(
        "startup.wide_open — running with: %s. Fine for local dev; before "
        "exposing this beyond localhost, set at least API_AUTH_TOKEN or "
        "JWT_SECRET. See the README's Security/Deployment guidance.",
        "; ".join(off),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    setup_tracing(app)
    # Re-evaluate now that .env is loaded (the limiter was constructed at import,
    # possibly before load_dotenv ran).
    limiter.enabled = rate_limiting_enabled()
    _warn_if_wide_open()
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
    conversation_id: int | None = None,
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
    #
    # When `conversation_id` is given (the ask/ask-stream paths, whose
    # `prior_messages` is always the FULL history so far), the previous
    # summary is cached and only the messages that newly aged out of the
    # recent window are folded in — so a long thread's summarizer call stays
    # cheap turn after turn instead of re-summarizing the whole older history
    # from scratch every single time. Callers with a partial/reconstructed
    # `prior_messages` (regenerate, edit) omit `conversation_id` and always
    # summarize from scratch, since their "older" boundary doesn't line up
    # with the cache.
    summary = ""
    if older_messages and _summarize_history_enabled():
        summarizer = summarize if summarize is not None else summarize_text
        cached = (
            get_summary_cache(conversation_id) if conversation_id is not None else None
        )
        if cached and int(cached["older_count"]) <= len(older_messages):
            new_older = older_messages[int(cached["older_count"]) :]
            summary = (
                summarize_conversation(
                    new_older, summarizer, previous_summary=str(cached["summary"])
                )
                if new_older
                else str(cached["summary"])
            )
        else:
            summary = summarize_conversation(older_messages, summarizer)
        if conversation_id is not None:
            set_summary_cache(conversation_id, len(older_messages), summary)

    # older_messages existing but summary still empty means summarization was
    # needed and attempted but yielded nothing usable (disabled, no cached
    # fallback, or a swallowed failure deep in summarize_text /
    # summarize_conversation) — the model is missing that older context, so it
    # must not be told to assume it has the full picture.
    context_incomplete = bool(older_messages) and not summary

    lines = [
        "You are continuing a saved conversation.",
        "Use the conversation history below when it is relevant.",
        (
            "Some earlier messages in this conversation happened before the "
            "history shown below and could not be summarized here — if the "
            "user asks about something from that period, say you don't have "
            "it rather than guessing or claiming there is no earlier history."
            if context_incomplete
            else "Do not claim you lack context if the answer is present in the history."
        ),
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


# How many recent turns the ambiguity classifier sees — enough to catch a
# "this"/"that" referring back a turn or two, small enough to stay a cheap
# addition to the same classifier call rather than a meaningfully bigger one.
_AMBIGUITY_HISTORY_TURNS = 4


def build_recent_history_snippet(
    prior_messages: list[dict[str, Any]], turns: int = _AMBIGUITY_HISTORY_TURNS
) -> str:
    """A short "ROLE: content" snippet of the last few turns, for the router's
    ambiguity check only (see routing.decide_route) — never used to build the
    actual answering prompt. Each line capped so one long past message can't
    blow up the classifier prompt; empty string when there's no history yet,
    the same "nothing to be ambiguous against" case the classifier treats as
    never ambiguous."""
    lines = []
    for message in prior_messages[-turns:]:
        role = str(message.get("role", "unknown")).strip()
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{role.upper()}: {content[:300]}")
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


def _encode_code_results(code_results: list[CodeResult] | None) -> str | None:
    """A message's code_interpreter tool calls as a JSON string, or None."""
    if not code_results:
        return None
    return json.dumps([c.model_dump() for c in code_results])


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


def _require_admin(owner: str | None) -> None:
    """Block settings mutation from an untrusted self-registered account.

    This app has no other admin/role concept — every authenticated caller is
    otherwise equally privileged. That's fine when the user set is
    operator-provisioned (registration closed) or auth is a single shared
    static token, but with JWT auth enabled AND open registration, anyone can
    self-register their own credential (see registration_allowed()) and would
    otherwise inherit the same settings-write rights as the operator. Gate
    only that one path; every other configuration keeps today's behavior.
    """
    if not jwt_enabled() or not registration_allowed():
        return
    admins = admin_usernames()
    if not admins or owner is None or owner.strip().lower() not in admins:
        raise HTTPException(
            status_code=403,
            detail=(
                "Settings editing requires an admin account while open "
                "registration is enabled. Set ADMIN_USERNAMES, or set "
                "ALLOW_REGISTRATION=false."
            ),
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
def put_setting(
    key: str, req: SettingUpdate, owner: str | None = Depends(current_owner)
):
    """Set a model or feature-flag override for a key, or clear it when the
    value is empty."""
    _require_writable_settings()
    _require_admin(owner)
    _require_settable_key(key)

    validator = (
        validate_bool_value if key in FEATURE_FLAG_KEYS else validate_model_value
    )
    try:
        value = validator(req.value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if value:
        set_setting(key, value)
    else:
        delete_setting(key)

    return describe_settings()


@router.delete("/v1/settings/{key}")
def clear_setting(key: str, owner: str | None = Depends(current_owner)):
    """Clear a single override, reverting the key to its env var / default."""
    _require_writable_settings()
    _require_admin(owner)
    _require_settable_key(key)
    delete_setting(key)
    return describe_settings()


@router.post("/v1/settings/reset")
def reset_settings(owner: str | None = Depends(current_owner)):
    """Clear every override, reverting the whole map to env vars / defaults."""
    _require_writable_settings()
    _require_admin(owner)
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
            research=req.research,
        )
    if pin:
        return AskRequest(
            question=question,
            mode=Mode.smart,
            no_cache=req.no_cache,
            model=pin,
            images=req.images,
            files=req.files,
            research=req.research,
        )
    return AskRequest(
        question=question,
        mode=req.mode,
        no_cache=req.no_cache,
        model=req.model,
        images=req.images,
        files=req.files,
        research=req.research,
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
    refusal, reservation_id = budget.reserve(model, 0, extra_cost_usd=cost, owner=owner)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        text = transcribe_audio(req.audio)
    except TranscriptionError as err:
        budget.release(reservation_id)
        raise HTTPException(status_code=502, detail=str(err)) from err
    if reservation_id is not None:
        finalize_spend(reservation_id, 0, 0, cost)
    else:
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
    refusal, reservation_id = budget.reserve(model, 0, extra_cost_usd=cost, owner=owner)
    if refusal is not None:
        raise HTTPException(status_code=402, detail=refusal)
    try:
        audio = synthesize_speech(req.text)
    except SpeechError as err:
        budget.release(reservation_id)
        raise HTTPException(status_code=502, detail=str(err)) from err
    if reservation_id is not None:
        finalize_spend(reservation_id, 0, 0, cost)
    else:
        record_spend(owner, model, 0, 0, cost)
    return Response(content=audio, media_type="audio/mpeg")


@router.get("/v1/search", response_model=list[SearchResult])
def search(
    q: str = Query(..., min_length=1, max_length=200),
    owner: str | None = Depends(current_owner),
):
    """Search this owner's conversations by title or message content."""
    return search_conversations(owner, q)


@router.get("/v1/bookmarks", response_model=list[BookmarkedMessage])
def bookmarks(owner: str | None = Depends(current_owner)):
    """Every bookmarked message across this owner's conversations, newest
    first, so a bookmark set on any one conversation is reviewable in one
    place instead of only visible while that conversation happens to be
    open.
    """
    return list_bookmarked_messages(owner)


@router.get("/v1/templates", response_model=list[TemplateOut])
def templates(owner: str | None = Depends(current_owner)):
    """This owner's saved prompt templates, most-recently-updated first —
    reusable snippets insertable into any conversation's composer, distinct
    from a single conversation's own Custom Instructions."""
    return list_templates(owner)


@router.post("/v1/templates", response_model=TemplateOut, status_code=201)
def create_template_endpoint(
    req: TemplateCreate, owner: str | None = Depends(current_owner)
):
    return create_template(owner, req.name, req.content)


def _owned_template_or_404(template_id: int, owner: str | None) -> dict:
    template = get_template(template_id)
    if template is None or template["owner"] != owner:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.patch("/v1/templates/{template_id}", response_model=TemplateOut)
def update_template_endpoint(
    template_id: int,
    req: TemplateUpdate,
    owner: str | None = Depends(current_owner),
):
    _owned_template_or_404(template_id, owner)
    if req.name is None and req.content is None:
        raise HTTPException(
            status_code=400, detail="Provide a name and/or content to update"
        )
    updated = update_template(template_id, name=req.name, content=req.content)
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return updated


@router.delete("/v1/templates/{template_id}")
def delete_template_endpoint(
    template_id: int, owner: str | None = Depends(current_owner)
):
    _owned_template_or_404(template_id, owner)
    delete_template(template_id)
    return {"status": "deleted", "template_id": template_id}


@router.get("/v1/usage", response_model=UsageSummary)
def usage(
    days: int = Query(default=14, ge=1, le=90),
    owner: str | None = Depends(current_owner),
):
    """This caller's own spend: today's total, by-model breakdown, by-day
    series — plus the configured cap(s) and how much of THIS caller's own
    per-owner cap is left today. Never the live global total (see budget.py):
    only the configured limits, which aren't sensitive on their own.
    """
    summary = usage_summary(owner, days)
    owner_limit = daily_budget_per_owner_usd()
    summary["daily_budget_usd"] = daily_budget_usd()
    summary["daily_budget_per_owner_usd"] = owner_limit
    summary["owner_remaining_usd"] = (
        max(0.0, owner_limit - summary["today_usd"])
        if owner_limit is not None
        else None
    )
    return summary


@router.get("/v1/conversations", response_model=list[ConversationOut])
def conversations(
    include_archived: bool = False, owner: str | None = Depends(current_owner)
):
    return list_conversations(owner, include_archived)


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

    Builds a fresh conversation with new message ids and no model calls.
    Restores everything duplicate_conversation() also copies — pin,
    instructions, and per-message tokens/cost/cached/sources/truncated/
    code_results — since none of it is a binary blob; attachments
    (images/files) are the one exception and are deliberately not restored
    (see ConversationImport's docstring).
    """
    conversation_id = int(create_conversation(req.title, owner)["id"])
    if req.pinned_model:
        set_conversation_pin(conversation_id, req.pinned_model)
    if req.system_prompt:
        set_conversation_system_prompt(conversation_id, req.system_prompt)
    if req.favorite:
        set_conversation_favorite(conversation_id, True)
    if req.tags:
        set_conversation_tags(conversation_id, req.tags)
    for message in req.messages:
        add_message(
            conversation_id=conversation_id,
            role=message.role,
            content=message.content,
            mode_used=message.mode_used,
            notes=message.notes,
            input_tokens=message.input_tokens,
            output_tokens=message.output_tokens,
            cost_usd=message.cost_usd,
            cached=message.cached,
            sources=_encode_sources(message.sources),
            truncated=message.truncated,
            code_results=_encode_code_results(message.code_results),
        )

    return get_conversation(conversation_id)


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


@router.put(
    "/v1/conversations/{conversation_id}/favorite", response_model=ConversationOut
)
def favorite_conversation(
    conversation_id: int,
    req: ConversationFavorite,
    owner: str | None = Depends(current_owner),
):
    """Star (or unstar) a conversation, pinning it to the top of the sidebar."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_favorite(conversation_id, req.favorite)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put(
    "/v1/conversations/{conversation_id}/archive", response_model=ConversationOut
)
def archive_conversation(
    conversation_id: int,
    req: ConversationArchive,
    owner: str | None = Depends(current_owner),
):
    """Archive (or restore) a conversation, hiding it from the default
    sidebar list without deleting anything."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_archived(conversation_id, req.archived)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation


@router.put("/v1/conversations/{conversation_id}/tags", response_model=ConversationOut)
def tag_conversation(
    conversation_id: int,
    req: ConversationTags,
    owner: str | None = Depends(current_owner),
):
    """Replace a conversation's freeform tags wholesale."""
    _owned_or_404(conversation_id, owner)
    conversation = set_conversation_tags(conversation_id, req.tags)
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


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/branch",
    response_model=ConversationOut,
)
def branch_conversation_endpoint(
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Branch a new conversation from this one, copying only the messages up
    to and including `message_id` — for exploring an alternate reply to an
    earlier point without disturbing the original conversation."""
    _owned_or_404(conversation_id, owner)
    conversation = branch_conversation(conversation_id, owner, message_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation or message not found")

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


@router.post(
    "/v1/conversations/{conversation_id}/messages/restore",
    response_model=MessageOut,
)
def restore_message(
    conversation_id: int,
    req: MessageRestoreRequest,
    owner: str | None = Depends(current_owner),
):
    """Recreate a single message (fresh id, no model call) in this
    conversation — the backing endpoint for Undo after deleting a message.
    Same fidelity as Import: everything but attachments comes back."""
    _owned_or_404(conversation_id, owner)
    return add_message(
        conversation_id=conversation_id,
        role=req.role,
        content=req.content,
        mode_used=req.mode_used,
        notes=req.notes,
        input_tokens=req.input_tokens,
        output_tokens=req.output_tokens,
        cost_usd=req.cost_usd,
        cached=req.cached,
        sources=_encode_sources(req.sources),
        truncated=req.truncated,
        code_results=_encode_code_results(req.code_results),
    )


@router.put(
    "/v1/conversations/{conversation_id}/messages/{message_id}/bookmark",
    response_model=MessageOut,
)
def bookmark_message(
    conversation_id: int,
    message_id: int,
    req: MessageBookmark,
    owner: str | None = Depends(current_owner),
):
    """Bookmark/unbookmark a single message — a marker on one turn, distinct
    from favoriting the whole conversation."""
    _owned_or_404(conversation_id, owner)
    updated = set_message_bookmarked(conversation_id, message_id, req.bookmarked)
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


def _continuation_prompt(prior_content: str) -> str:
    return (
        "Continue your previous answer in this conversation EXACTLY where it "
        "left off. Do not repeat any part of it, and do not add a preamble, "
        "acknowledgement, or restated heading — your reply must pick up "
        "mid-sentence (or mid-code) exactly as if the text below never "
        "stopped.\n\n"
        "Your answer so far, cut off mid-way:\n"
        f"{prior_content}"
    )


@router.post(
    "/v1/conversations/{conversation_id}/messages/{message_id}/continue",
    response_model=MessageOut,
)
@limiter.limit(rate_limit_value)
def continue_message(
    request: Request,
    conversation_id: int,
    message_id: int,
    owner: str | None = Depends(current_owner),
):
    """Resume a message that got cut off at max_output_tokens.

    Non-streaming by design (unlike ask/regenerate/edit): a continuation is a
    short, occasional follow-up action, not the primary answering path, so a
    second streaming implementation isn't worth the added surface here. The
    continuation is appended to the SAME message row rather than creating a
    new one — from the user's point of view they asked one question and got
    one (possibly multi-part) answer.
    """
    conversation = _owned_or_404(conversation_id, owner)
    messages = list_messages(conversation_id)
    target = next((m for m in messages if int(m["id"]) == message_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if str(target["role"]) != "assistant":
        raise HTTPException(
            status_code=400, detail="Only an assistant message can be continued"
        )
    if not target.get("truncated"):
        raise HTTPException(status_code=400, detail="Message was not truncated")

    prior = [m for m in messages if int(m["id"]) < message_id]
    context_question = build_context_prompt(
        prior_messages=prior,
        current_question=_continuation_prompt(str(target["content"])),
        system_prompt=conversation.get("system_prompt"),
    )
    base_req = AskRequest(question=context_question, mode=Mode.auto, no_cache=True)
    contextual_req = _pinned_ask_request(conversation, context_question, base_req)

    result = run_orchestrator(contextual_req, owner=owner)

    if not result.answer.strip():
        raise HTTPException(
            status_code=502, detail=result.notes or "Continuation failed"
        )

    updated = append_to_message(
        conversation_id=conversation_id,
        message_id=message_id,
        additional_content=result.answer,
        truncated=result.truncated,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return updated


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
        conversation_id=conversation_id,
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    # Route on the new user turn, not the assembled context prompt.
    result = run_orchestrator(
        contextual_req,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
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
        code_results=result.code_results,
        truncated=result.truncated,
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
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
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
        conversation_id=conversation_id,
    )

    contextual_req = _pinned_ask_request(conversation, context_question, req)

    context_note = f"context_messages={len(prior_messages)}"

    return _stream_and_persist(
        conversation_id,
        contextual_req,
        context_note,
        routing_question=req.question,
        owner=owner,
        history=build_recent_history_snippet(prior_messages),
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
    history: str = "",
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
        orchestrator_stream = stream_orchestrator(
            contextual_req, routing_question, owner, history=history
        )

        try:
            for event in orchestrator_stream:
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
                            action_status="pending"
                            if data.get("pending_action")
                            else None,
                            images=json.dumps(data["images"])
                            if data.get("images")
                            else None,
                            truncated=bool(data.get("truncated", False)),
                            code_results=json.dumps(data["code_results"])
                            if data.get("code_results")
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
        except GeneratorExit:
            # The client disconnected (Stop button, tab close, network drop)
            # mid-stream — Starlette closes this generator, raising
            # GeneratorExit at the `yield` above. Deterministically close the
            # inner generator now (not left to GC) so stream_orchestrator's own
            # GeneratorExit handling runs and records whatever spend it already
            # incurred, then persist whatever text streamed so far — same
            # treatment as a provider error mid-stream (the "error" branch
            # above): never silently drop a partial answer the user was
            # already reading.
            orchestrator_stream.close()
            partial = "".join(accumulated).strip()
            if replace_after_id is None and edit_message_id is None and partial:
                add_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=partial,
                    mode_used=mode_used,
                    notes=f"Interrupted before completion: client disconnected | {context_note}",
                )
            raise

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
        code_results=result.code_results,
        truncated=result.truncated,
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
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
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
        code_results=result.code_results,
        truncated=result.truncated,
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
            truncated=response.truncated,
            code_results=_encode_code_results(response.code_results),
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
