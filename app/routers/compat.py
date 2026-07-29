"""An OpenAI /v1/chat/completions-compatible endpoint: lets any tool built
against the OpenAI SDK/wire format (LangChain, an IDE plugin, curl, ...)
point at this app and inherit its routing, caching, and daily-budget
behavior instead of talking to OpenAI directly. Deliberately thin — it
translates the request/response shape and defers everything else (context
assembly, routing, caching, spend) to the exact same machinery
app/routers/ask.py and app/routers/messages.py already use.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..auth import current_owner
from ..orchestrator import run_orchestrator, stream_orchestrator
from ..ratelimit import limiter, rate_limit_value
from ..schemas import (
    AskRequest,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    Mode,
)
from ..settings import validate_model_value
from ..telemetry import new_request_meta
from .deps import router
from .messages import build_context_prompt, build_recent_history_snippet

# The routing-mode keywords a client can pass as `model`; anything else is
# treated as a request to force that exact model (see _resolve_model).
_MODE_VALUES = {mode.value for mode in Mode}


def _resolve_model(model: str) -> tuple[Mode, str | None]:
    """Turn ChatCompletionRequest.model into the (mode, forced-model) shape
    AskRequest expects — same convention as a conversation's model pin (see
    app/routers/messages.py's _pinned_ask_request): one of the routing-mode
    keywords selects that tier, anything else forces that exact model
    (bypassing routing and the cache) with the generous smart-tier budget.
    Raises HTTPException(400) for a malformed forced-model name."""
    cleaned = model.strip()
    if cleaned in _MODE_VALUES:
        return Mode(cleaned), None
    try:
        validated = validate_model_value(cleaned)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Mode.smart, validated or None


def _build_prompt(req: ChatCompletionRequest) -> tuple[str, str, bool]:
    """Split the OpenAI-shaped `messages` array into (context_question,
    routing_question, context_free) the same way a conversation's own
    history does — every message but the last becomes prior context, the
    last (validated as role=user) becomes the new question. System messages
    are joined into one instructions block, mirroring a conversation's
    system_prompt."""
    *prior, last = req.messages
    routing_question = last.content
    system_prompt = "\n\n".join(m.content for m in prior if m.role == "system")
    prior_messages = [
        {"role": m.role, "content": m.content} for m in prior if m.role != "system"
    ]
    context_question = build_context_prompt(
        prior_messages=prior_messages,
        current_question=routing_question,
        system_prompt=system_prompt or None,
    )
    context_free = not prior_messages and not system_prompt
    return context_question, routing_question, context_free


@router.post("/v1/chat/completions")
@limiter.limit(rate_limit_value)
def chat_completions(
    request: Request,
    req: ChatCompletionRequest,
    owner: str | None = Depends(current_owner),
):
    context_question, routing_question, context_free = _build_prompt(req)
    mode, forced_model = _resolve_model(req.model)
    contextual_req = AskRequest(
        question=context_question,
        mode=mode,
        model=forced_model,
    )
    history = build_recent_history_snippet(
        [{"role": m.role, "content": m.content} for m in req.messages[:-1]]
    )

    if req.stream:
        return _stream_chat_completion(
            contextual_req, routing_question, owner, history, context_free, req.model
        )

    meta = new_request_meta()
    result = run_orchestrator(
        contextual_req,
        routing_question=routing_question,
        owner=owner,
        history=history,
        context_free=context_free,
    )
    return ChatCompletionResponse(
        id=f"chatcmpl-{meta.request_id}",
        created=int(time.time()),
        model=req.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=result.answer),
                finish_reason="length" if result.truncated else "stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=result.input_tokens or 0,
            completion_tokens=result.output_tokens or 0,
            total_tokens=(result.input_tokens or 0) + (result.output_tokens or 0),
        ),
    )


def _stream_chat_completion(
    contextual_req: AskRequest,
    routing_question: str,
    owner: str | None,
    history: str,
    context_free: bool,
    requested_model: str,
) -> StreamingResponse:
    meta = new_request_meta()
    completion_id = f"chatcmpl-{meta.request_id}"
    created = int(time.time())

    def _chunk(delta: dict[str, str], finish_reason: str | None) -> str:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": requested_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(payload)}\n\n"

    def event_stream() -> Iterator[str]:
        yield _chunk({"role": "assistant"}, None)
        truncated = False
        for event in stream_orchestrator(
            contextual_req,
            routing_question,
            owner,
            history=history,
            context_free=context_free,
        ):
            name = str(event["event"])
            data = dict(event["data"])
            if name == "delta":
                text = str(data.get("text", ""))
                if text:
                    yield _chunk({"content": text}, None)
            elif name == "done":
                truncated = bool(data.get("truncated", False))
            elif name == "error":
                # No OpenAI-native error-mid-stream chunk shape to match here;
                # end the stream the same way a provider outage does on the
                # real API — a final chunk, then [DONE], nothing pretending
                # to be a successful finish_reason.
                break
        yield _chunk({}, "length" if truncated else "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
