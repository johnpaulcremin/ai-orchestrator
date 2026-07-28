from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Generator, Iterator
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from openai import BadRequestError

from . import budget, cache, database
from .actions import actions_enabled, named_webhooks
from .observability import enrich_span
from .providers import (
    AUTH_ERRORS,
    RATE_ERRORS,
    call_anthropic,
    call_litellm,
    generate_images_litellm,
    key_env_for,
    provider_of,
    stream_anthropic,
    stream_litellm,
)
from .routing import RouteDecision, decide_route
from .schemas import (
    AskRequest,
    AskResponse,
    CodeResult,
    FileAttachment,
    PendingAction,
    Source,
)
from .settings import bool_setting, model_setting
from .telemetry import elapsed_ms, logger, new_request_meta
from .usage import (
    Usage,
    estimate_code_execution_cost,
    estimate_cost,
    estimate_image_cost,
)

load_dotenv()

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Create the OpenAI client on first use so the module imports without a key."""
    global _client

    if _client is None:
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Check your .env and shell env vars."
            )
        _client = OpenAI(api_key=api_key)

    return _client


def _timeout_seconds() -> float:
    """Request timeout for answer calls. Tolerates missing or malformed values."""
    raw = (os.getenv("OPENAI_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 120.0
    return value if value > 0 else 120.0


def _vendor_of(model: str) -> str:
    """A finer vendor identity than provider_of, for cross-vendor failover.

    provider_of buckets every LiteLLM-routed model (gemini/…, mistral/…,
    bedrock/…, groq/…) as "litellm", which would make two genuinely different
    vendors look identical. Here a provider-prefixed model keeps its prefix, so
    gemini/… and mistral/… count as different vendors (independent keys/quotas).
    """
    name = (model or "").strip().lower()
    if "/" in name:
        return name.split("/", 1)[0]
    return provider_of(name)


def _fallback_models(
    primary_model: str, cross_provider_only: bool = False
) -> list[str]:
    """
    Ordered fallback candidates, cross-provider first.

    OPENAI_MODEL_FALLBACK is optional. If it is not set, we fall back to
    OPENAI_MODEL_FAST, then OPENAI_MODEL. Duplicates and the primary model are
    removed. Candidates whose provider differs from the primary's are tried
    FIRST, so a provider-wide outage (or a throttled key) is more likely to be
    escaped — set e.g. OPENAI_MODEL_FALLBACK=claude-sonnet-5 for a genuinely
    independent fallback. With `cross_provider_only=True` (rate-limit failover,
    where the same key would just be throttled again) same-provider candidates
    are dropped entirely.
    """
    # Resolve through the settings layer so a saved override for any of these
    # keys is honoured in the fallback chain, not just the env var. Mirror
    # routing's defaults: FAST falls back to the base model, and the base keeps
    # its "gpt-5" code default so it's always a final fallback candidate (without
    # it, overriding only a tier while leaving OPENAI_MODEL unset would leave the
    # chain empty).
    base = model_setting("OPENAI_MODEL", "gpt-5")
    candidates = [
        model_setting("OPENAI_MODEL_FALLBACK"),
        model_setting("OPENAI_MODEL_FAST", base),
        base,
    ]

    primary_vendor = _vendor_of(primary_model)
    seen: set[str] = set()
    cross: list[str] = []
    same: list[str] = []

    for model in candidates:
        if not model or model == primary_model or model in seen:
            continue
        seen.add(model)
        if _vendor_of(model) != primary_vendor:
            cross.append(model)
        else:
            same.append(model)

    return cross if cross_provider_only else cross + same


def _record_spend(
    owner: str | None,
    model: str,
    usage: Usage,
    extra_cost_usd: float = 0.0,
    reservation_id: int | None = None,
) -> None:
    """Best-effort spend-log write for a completed call (never breaks the answer).

    Recorded even when the answer is empty/truncated, as long as tokens were
    spent, so the daily budget accounts for calls not persisted as messages.
    `extra_cost_usd` folds in non-token costs (currently: generated images).

    `reservation_id` (from budget.reserve()) is the pre-dispatch placeholder
    row to reconcile rather than inserting a fresh one — see try_reserve_spend
    in database.py. A call with no usage and no extra cost genuinely spent
    nothing, so any held reservation is released instead of finalized.
    """
    if not usage.total_tokens and not extra_cost_usd:
        budget.release(reservation_id)
        return
    try:
        cost = estimate_cost(model, usage)
        if extra_cost_usd:
            # Only force None (unpriced) to 0.0 when there's an extra cost to
            # fold in — an unpriced model with no extra cost stays None
            # (unknown), never a misleading "free".
            cost = (cost or 0.0) + extra_cost_usd
        if reservation_id is not None:
            database.finalize_spend(
                reservation_id, usage.input_tokens, usage.output_tokens, cost
            )
        else:
            database.record_spend(
                owner, model, usage.input_tokens, usage.output_tokens, cost
            )
    except Exception:
        logger.exception("spend.record_failed model=%s", model)


def _record_avoided_cost(owner: str | None, hit: dict, reason: str) -> None:
    """Best-effort avoided-cost-log write for a response-cache hit (never
    breaks the answer). `hit` is the cache entry itself — its own `cost_usd`
    is what THIS call would have cost had it gone live instead of being
    served from cache, so that's exactly what gets logged as avoided.
    """
    avoided_cost = hit.get("cost_usd")
    if not isinstance(avoided_cost, (int, float)):
        avoided_cost = None
    try:
        database.record_avoided_cost(owner, hit.get("model"), reason, avoided_cost)
    except Exception:
        logger.exception("avoided_cost.record_failed reason=%s", reason)


def _extract_text(result: object) -> str:
    """The response's output text, or '' when the model produced none.

    A model can return an empty-output response WITHOUT raising (e.g. a reasoning
    call truncated before any output — status 'incomplete', HTTP 200). Return ''
    in that case, never the object's repr, so callers and persistence treat it as
    the empty answer it is (see the empty-answer guards in main.py) instead of
    storing a 'Response(...)' string as the assistant reply.
    """
    answer_text = getattr(result, "output_text", None) or ""
    if not answer_text:
        # Log the raw object for debugging, but never return it as the answer.
        logger.warning("response.no_output_text result=%r", str(result)[:200])
        return ""
    return answer_text.strip()


_SUMMARY_PROMPT = (
    "Summarize the earlier part of a conversation into compact notes the "
    "assistant can rely on later. Preserve facts, decisions, names, numbers, and "
    "anything the user might refer back to. Be concise and omit pleasantries.\n\n"
    "Conversation excerpt:\n{text}"
)

# Cap on the transcript fed to the summarizer, to bound cost. When the older
# window is larger than this, keep the TAIL (the most recent of the older turns,
# which are the most relevant) rather than truncating to the oldest.
_SUMMARY_INPUT_CHARS = 24000


def _summary_max_tokens() -> int:
    raw = (os.getenv("SUMMARY_MAX_OUTPUT_TOKENS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 600
    return value if value > 0 else 600


def _run_summary_call(prompt: str) -> str:
    """Shared plumbing for a one-shot, cheap-router-model summarization call.
    Returns '' on any failure (missing key, timeout, provider error) — every
    caller treats summarization as best-effort, never a hard dependency.
    """
    try:
        client = get_client()
    except RuntimeError:
        return ""

    router_model = model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano")
    # Best-effort + on the pre-answer critical path: fail fast (no SDK retries)
    # and a modest timeout, so a slow endpoint can't stall the answer for long.
    timeout_client = client.with_options(timeout=12.0, max_retries=0)

    def _create(**extra: Any) -> object:
        return timeout_client.responses.create(
            model=router_model,
            input=prompt,
            max_output_tokens=_summary_max_tokens(),
            **extra,
        )

    try:
        # Minimal reasoning keeps the summary call cheap, like the router.
        result = _create(reasoning={"effort": "minimal"})
    except BadRequestError:
        try:
            result = _create()
        except Exception:
            return ""
    except Exception:
        return ""

    return (getattr(result, "output_text", None) or "").strip()


def summarize_text(text: str) -> str:
    """Summarize text with the cheap router model. Returns '' on any failure.

    Used to fold older conversation turns into a memory summary. It never raises,
    so a missing key / model error simply omits the summary.
    """
    clean = (text or "").strip()
    if not clean:
        return ""
    # Keep the most recent slice of the older window (see _SUMMARY_INPUT_CHARS).
    prompt = _SUMMARY_PROMPT.format(text=clean[-_SUMMARY_INPUT_CHARS:])
    return _run_summary_call(prompt)


_DISPLAY_SUMMARY_PROMPT = (
    "Write a short, user-facing TL;DR of this conversation: the key topics "
    "discussed, any decisions or conclusions reached, and any open questions "
    "still unresolved. Plain prose or a short bulleted list, whichever reads "
    "better. No meta-commentary about being a summary — just the recap "
    "itself.\n\n"
    "Conversation:\n{text}"
)


def summarize_conversation_for_display(messages: list[dict[str, Any]]) -> str:
    """A short, user-facing TL;DR of a whole conversation — backs the 🧾
    Summarize button. Distinct from summarize_text, whose terse, fact-dense
    notes are meant to feed back into the model as context, not to be read
    directly by a person. Returns '' on any failure (missing key, model
    error, or a conversation with no message content to summarize).
    """
    lines = []
    for message in messages:
        role = str(message.get("role", "")).strip().upper()
        content = str(message.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    transcript = "\n".join(lines)
    if not transcript:
        return ""
    prompt = _DISPLAY_SUMMARY_PROMPT.format(text=transcript[-_SUMMARY_INPUT_CHARS:])
    return _run_summary_call(prompt)


class _ModelStreamError(Exception):
    """Raised when the streaming API reports a terminal failure event."""


def _usage_fields(model: str, usage: Usage, extra_cost_usd: float = 0.0) -> dict:
    """AskResponse/done-event usage fields, or empty if no tokens were captured.

    `extra_cost_usd` folds in non-token costs (currently: generated images).
    """
    if usage.total_tokens == 0 and not extra_cost_usd:
        return {}
    cost = estimate_cost(model, usage)
    if extra_cost_usd:
        cost = (cost or 0.0) + extra_cost_usd
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost,
    }


def _record_openai_usage(result: object, usage: Usage | None) -> None:
    if usage is None:
        return
    source = getattr(result, "usage", None)
    if source is not None:
        usage.input_tokens = int(getattr(source, "input_tokens", 0) or 0)
        usage.output_tokens = int(getattr(source, "output_tokens", 0) or 0)
        # Prompt tokens OpenAI served from its cache (billed at a discount).
        details = getattr(source, "input_tokens_details", None)
        if details is not None:
            usage.cached_input_tokens = int(getattr(details, "cached_tokens", 0) or 0)


# A web citation from the web_search tool: {"title": str, "url": str}.
Citation = dict[str, str]

# Cap on how many citations are kept per answer — a search can return dozens;
# only the first few are worth surfacing to the user.
_MAX_CITATIONS = 8

_WEB_SEARCH_TOOL: dict[str, Any] = {"tools": [{"type": "web_search"}]}


def _extract_citations(result: object) -> list[Citation]:
    """Pull url_citation annotations out of a Response's output items.

    De-duplicated by URL, in first-seen order, capped at _MAX_CITATIONS. Never
    raises — citations are an enrichment, not something worth failing an answer
    over if the SDK's shape ever changes underneath us. Only http(s) URLs are
    kept: this is the single point every citation passes through (persisted
    history and the live SSE frame alike), so it's the one place to block a
    javascript:/data: URL that page content fetched by the search tool could
    have injected into the model's output before it ever reaches a rendered
    <a href> — React escapes text but not attribute values.
    """
    citations: list[Citation] = []
    seen: set[str] = set()
    try:
        for item in getattr(result, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                for annotation in getattr(content, "annotations", None) or []:
                    if getattr(annotation, "type", None) != "url_citation":
                        continue
                    url = getattr(annotation, "url", "") or ""
                    if not url or url in seen:
                        continue
                    if not url.lower().startswith(("http://", "https://")):
                        continue
                    seen.add(url)
                    citations.append(
                        {"title": getattr(annotation, "title", "") or url, "url": url}
                    )
                    if len(citations) >= _MAX_CITATIONS:
                        return citations
    except Exception:
        logger.exception("citations.extract_failed")
        return []
    return citations


# A proposed real-world action: {"action": str, "summary": str, "payload": dict}.
# Propose-then-confirm: extracting this NEVER executes anything — see app/actions.py.
PendingActionDict = dict[str, object]


def _build_action_tool() -> dict[str, Any]:
    """The propose_action function tool, with its `action` field restricted
    to an enum of the operator's actual configured named routes (see
    actions.named_webhooks) when any exist — so the model can only ever
    propose an action type that has somewhere real to go, instead of
    inventing a name that silently falls through to the catch-all webhook (or
    nowhere, if there isn't one). Falls back to a freeform string, unchanged
    from before named routes existed, when ACTIONS_WEBHOOKS isn't set.
    """
    routes = named_webhooks()
    action_property: dict[str, Any] = (
        {
            "type": "string",
            "enum": sorted(routes),
            "description": "Which configured action type this is.",
        }
        if routes
        else {
            "type": "string",
            "description": "Short action type, e.g. 'send_email', 'update_sheet', 'post_message'.",
        }
    )
    return {
        "tools": [
            {
                "type": "function",
                "name": "propose_action",
                "description": (
                    "Propose a real-world action on the user's behalf (e.g. send an "
                    "email, add a row to a spreadsheet, post a message). This does "
                    "NOT execute anything — it only records a proposal. The user "
                    "must explicitly confirm it in the UI before anything happens. "
                    "Only call this when the user has actually asked for something "
                    "to be done in the outside world, not for routine questions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": action_property,
                        "summary": {
                            "type": "string",
                            "description": "One sentence describing what this action will do, shown to the user for approval.",
                        },
                        "payload": {
                            "type": "object",
                            "description": "Structured data the action needs (e.g. recipient, subject, body).",
                        },
                    },
                    "required": ["action", "summary", "payload"],
                },
                "strict": False,
            }
        ]
    }


def _action_confirmation_note(action: PendingActionDict) -> str:
    return (
        f"I've prepared an action for your review: **{action['summary']}**. "
        "Confirm below to run it."
    )


def _extract_pending_action(result: object) -> PendingActionDict | None:
    """Pull a propose_action function-call out of a Response's output items.

    Returns the FIRST valid one found (a single answer proposes at most one
    action in this design) or None. Never raises — a malformed/unexpected tool
    call degrades to "no action proposed" rather than failing the answer.
    """
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "function_call":
                continue
            if getattr(item, "name", None) != "propose_action":
                continue
            raw_args = getattr(item, "arguments", None) or ""
            try:
                args = json.loads(raw_args)
            except (ValueError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            action = str(args.get("action", "")).strip()
            summary = str(args.get("summary", "")).strip()
            payload = args.get("payload")
            if not action or not summary or not isinstance(payload, dict):
                continue
            return {"action": action, "summary": summary, "payload": payload}
    except Exception:
        logger.exception("actions.extract_failed")
        return None
    return None


def _compose_answer_with_notes(model_text: str, notes: list[str]) -> str:
    """Append synthesized notes (action confirmation, image caption, ...) to the
    model's own text, or return the notes alone when the model produced none.

    A model that calls a hosted/function tool commonly returns NO text at all.
    Without this, a tool-only reply would look like an empty answer and get
    silently dropped by the empty-answer guards (see main.py).
    """
    if not notes:
        return model_text
    combined = "\n\n".join(notes)
    return f"{model_text}\n\n{combined}" if model_text else combined


def _image_generation_note(count: int) -> str:
    return (
        "Here's the image you asked for."
        if count == 1
        else f"Here are the {count} images you asked for."
    )


# Substrings indicating a BadRequest is about the REQUEST ITSELF (a moderated
# question, an over-length prompt, ...), not an unsupported optional param —
# retrying with reasoning/web_search stripped would just repeat the identical
# failure. Best-effort and deliberately conservative: an error that doesn't
# match still gets the retry-and-drop treatment, so a genuine param rejection
# phrased differently is never given up on early (that would break the whole
# point of the ladder — answering without a search instead of failing).
_NON_PARAM_BADREQUEST_MARKERS = (
    "moderation",
    "content_policy",
    "invalid_prompt",
    "flagged",
    "context_length",
    "maximum context length",
    "too long",
)


def _looks_param_related(error: BaseException) -> bool:
    text = str(error).lower()
    return not any(marker in text for marker in _NON_PARAM_BADREQUEST_MARKERS)


def _build_input(
    question: str,
    attachments: list[str] | None,
    files: list[FileAttachment] | None = None,
) -> str | list[dict[str, Any]]:
    """The Responses API `input`: plain text, or a single user message with a
    text part plus an image part per attachment (data:image/...;base64,... URLs
    — vision input) and/or a file part per document (PDF/plain text) when the
    user attached one or more images/files."""
    if not attachments and not files:
        return question
    content: list[dict[str, Any]] = [{"type": "input_text", "text": question}]
    content.extend(
        {"type": "input_image", "image_url": url} for url in attachments or []
    )
    content.extend(
        {"type": "input_file", "filename": file.filename, "file_data": file.data}
        for file in files or []
    )
    return [{"role": "user", "content": content}]


def _create_with_fallback(
    client: OpenAI,
    model: str,
    question: str,
    max_output_tokens: int,
    attempts: list[dict[str, Any]],
    *,
    stream: bool = False,
    attachments: list[str] | None = None,
    files: list[FileAttachment] | None = None,
) -> object:
    """Try each `extra` kwargs dict in `attempts`, richest first.

    A BadRequest (an unsupported param for this model, e.g. reasoning or
    web_search) drops it and retries the next, simpler combination. The last
    attempt (always `{}` in practice) is never caught, so a genuine failure
    still propagates to the caller's own error handling — and a BadRequest that
    plausibly isn't about an optional param at all (e.g. a moderated question)
    re-raises immediately instead of repeating the same failure 2-3 more times.
    """
    input_value = _build_input(question, attachments, files)
    for index, extra in enumerate(attempts):
        try:
            return client.responses.create(
                model=model,
                input=input_value,  # type: ignore[arg-type]
                max_output_tokens=max_output_tokens,
                stream=stream,
                **extra,
            )
        except BadRequestError as err:
            is_last = index == len(attempts) - 1
            if is_last or not _looks_param_related(err):
                raise
            logger.warning(
                "responses.param_rejected model=%s stream=%s params=%s",
                model,
                stream,
                sorted(extra),
            )
    raise AssertionError(  # pragma: no cover - unreachable: see docstring
        "unreachable: the last attempt always either succeeds or re-raises"
    )


def _image_generation_enabled() -> bool:
    """Opt-in: IMAGE_GENERATION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) turns on image
    generation.

    Which code path is used depends on _image_generation_provider(): the
    OpenAI path offers a tool and lets the model decide when to call it (same
    as propose_action); the Gemini path has no such tool, so it's gated by
    _looks_like_image_request instead. Off by default either way.
    """
    return bool_setting("IMAGE_GENERATION", False)


def _image_generation_model() -> str:
    return (os.getenv("IMAGE_GENERATION_MODEL") or "").strip() or "gpt-image-1"


def _image_generation_provider() -> str:
    """ "openai" (the built-in Responses API tool) or "gemini" (a standalone
    LiteLLM image_generation call, since Gemini/Imagen has no equivalent of a
    tool the chat model can call itself) — selected by IMAGE_GENERATION_MODEL's
    prefix, the same "prefix picks the provider" convention used everywhere
    else in this app (OPENAI_MODEL_FAST=gemini/... routes through LiteLLM too).
    """
    return (
        "gemini"
        if _image_generation_model().strip().lower().startswith("gemini/")
        else "openai"
    )


_IMAGE_GENERATION_QUALITIES = {"low", "medium", "high", "auto"}


def _image_generation_quality() -> str:
    # Default "high": once an operator opts in, best-effort quality is the
    # point — cost-sensitive deployments can override this down.
    raw = (os.getenv("IMAGE_GENERATION_QUALITY") or "high").strip().lower()
    return raw if raw in _IMAGE_GENERATION_QUALITIES else "high"


def _image_generation_size() -> str:
    return (os.getenv("IMAGE_GENERATION_SIZE") or "").strip() or "auto"


def _worst_case_image_cost(images_wanted: bool, gemini_image_wanted: bool) -> float:
    """Pre-dispatch budget estimate for this call's possible image generation.

    Neither gate guarantees an image actually gets generated (the OpenAI tool
    is only offered, not forced; the Gemini path always requests exactly one),
    but the budget gate already prices every call at its worst case (the full
    output token budget, even if the model uses less) — assuming one image
    here when either path is live is the same philosophy, not a new one.
    """
    if not (images_wanted or gemini_image_wanted):
        return 0.0
    return estimate_image_cost(1, _image_generation_quality()) or 0.0


def _build_image_generation_tool() -> dict[str, Any]:
    return {
        "type": "image_generation",
        "model": _image_generation_model(),
        "quality": _image_generation_quality(),
        "size": _image_generation_size(),
    }


# A deliberately narrow, high-precision phrase list used ONLY to trigger the
# separate Gemini/Imagen image-generation call (see _image_generation_provider)
# — Gemini has no equivalent of OpenAI's image_generation tool a chat model can
# call itself, so something has to decide when an image is actually wanted.
# Unlike web search's live-data heuristic, an image request is rarely ambiguous
# phrasing, so a phrase list is adequate here (not just an outage fallback).
_IMAGE_REQUEST_PHRASES = (
    "draw me",
    "draw a",
    "draw an",
    "generate an image",
    "generate a image",
    "generate a picture",
    "generate a photo",
    "generate artwork",
    "create an image",
    "create a picture",
    "create a photo",
    "create artwork",
    "make me an image",
    "make me a picture",
    "make an image",
    "make a picture",
    "paint a picture",
    "paint me",
    "illustrate a",
    "illustrate an",
    "sketch a",
    "sketch an",
    "design a logo",
    "generate a logo",
    "create a logo",
)


def _looks_like_image_request(question: str) -> bool:
    """Errs toward missing a request over over-triggering an extra paid call."""
    text = " ".join((question or "").lower().split())
    return any(phrase in text for phrase in _IMAGE_REQUEST_PHRASES)


def _extract_images(result: object) -> list[str]:
    """Pull completed image_generation_call results out of a Response's output
    items, as ready-to-render `data:image/png;base64,...` URLs.

    Never raises — an enrichment, not worth failing the answer over if the
    SDK's shape ever changes underneath us. Only "completed" calls with a
    result are kept; a "failed"/"in_progress" call has no image to show.
    """
    images: list[str] = []
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "image_generation_call":
                continue
            if getattr(item, "status", None) != "completed":
                continue
            data = getattr(item, "result", None)
            if data:
                images.append(f"data:image/png;base64,{data}")
    except Exception:
        logger.exception("images.extract_failed")
        return []
    return images


def _code_execution_enabled() -> bool:
    """Opt-in: CODE_EXECUTION=true (env, or a saved Settings override — same
    override > env > default chain as any model tier) lets the model run
    Python via OpenAI's hosted code_interpreter tool — a sandboxed container
    in OpenAI's own cloud, never on this machine, same trust boundary as
    web_search/image_generation. The model decides for itself when running
    code would help (verifying a calculation, testing a snippet), same as
    propose_action/image_generation. Off by default.
    """
    return bool_setting("CODE_EXECUTION", False)


_CODE_INTERPRETER_TOOL: dict[str, Any] = {
    "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]
}

CodeResultDict = dict[str, object]


def _extract_code_results(result: object) -> list[CodeResultDict]:
    """Pull completed code_interpreter_call items out of a Response's output,
    as {"code": str, "logs": str | None, "images": [data URL, ...]}.

    Never raises — an enrichment, not worth failing the answer over if the
    SDK's shape ever changes underneath us. Only "completed" calls are kept;
    an "in_progress"/"failed" call has nothing useful to show.
    """
    results: list[CodeResultDict] = []
    try:
        for item in getattr(result, "output", None) or []:
            if getattr(item, "type", None) != "code_interpreter_call":
                continue
            if getattr(item, "status", None) != "completed":
                continue
            code = getattr(item, "code", None)
            if not code:
                continue
            logs_parts: list[str] = []
            images: list[str] = []
            for output in getattr(item, "outputs", None) or []:
                output_type = getattr(output, "type", None)
                if output_type == "logs":
                    text = getattr(output, "logs", "") or ""
                    if text:
                        logs_parts.append(text)
                elif output_type == "image":
                    url = getattr(output, "url", "") or ""
                    if url:
                        images.append(url)
            results.append(
                {
                    "code": code,
                    "logs": "\n".join(logs_parts) or None,
                    "images": images,
                }
            )
    except Exception:
        logger.exception("code_results.extract_failed")
        return []
    return results


def _code_execution_note(count: int) -> str:
    return (
        "Ran a snippet of code to help answer this."
        if count == 1
        else f"Ran {count} snippets of code to help answer this."
    )


def _build_tools(
    web_search: bool, actions: bool, images: bool = False, code_execution: bool = False
) -> dict[str, Any]:
    """The combined `tools` kwarg for however many optional tools are active.

    web_search, actions, images, and code_execution are independent features
    that all just add an entry to the SAME `tools` list the Responses API
    accepts — collapsing them here keeps the retry ladder below a single "has
    tools or not" dimension instead of a combinatorial one.
    """
    tools: list[dict[str, Any]] = []
    if web_search:
        tools.extend(_WEB_SEARCH_TOOL["tools"])
    if actions:
        tools.extend(_build_action_tool()["tools"])
    if images:
        tools.append(_build_image_generation_tool())
    if code_execution:
        tools.extend(_CODE_INTERPRETER_TOOL["tools"])
    return {"tools": tools} if tools else {}


def _answer_attempts(
    reasoning_effort: str,
    web_search: bool,
    actions: bool = False,
    images: bool = False,
    code_execution: bool = False,
) -> list[dict[str, Any]]:
    """The ordered (richest-first) param combinations for an answer call.

    Identical to the pre-web-search behaviour when every tool flag is False
    (exactly the reasoning-then-bare two-step retry already covered by
    existing tests).
    """
    has_tools = web_search or actions or images or code_execution
    tools = _build_tools(web_search, actions, images, code_execution)
    attempts: list[dict[str, Any]] = []
    if reasoning_effort:
        attempts.append({"reasoning": {"effort": reasoning_effort}, **tools})
    if reasoning_effort and has_tools:
        attempts.append({"reasoning": {"effort": reasoning_effort}})
    if has_tools:
        attempts.append(dict(tools))
    attempts.append({})
    return attempts


def _call_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    web_search: bool = False,
    citations: list[Citation] | None = None,
    actions: bool = False,
    pending_action: list[PendingActionDict] | None = None,
    images: bool = False,
    generated_images: list[str] | None = None,
    attachments: list[str] | None = None,
    files: list[FileAttachment] | None = None,
    truncated: list[bool] | None = None,
    code_execution: bool = False,
    code_results: list[CodeResultDict] | None = None,
) -> str:
    client = get_client().with_options(timeout=_timeout_seconds())
    attempts = _answer_attempts(
        reasoning_effort, web_search, actions, images, code_execution
    )

    result = _create_with_fallback(
        client,
        model,
        question,
        max_output_tokens,
        attempts,
        attachments=attachments,
        files=files,
    )
    _record_openai_usage(result, usage)
    if truncated is not None and getattr(result, "status", None) == "incomplete":
        truncated.append(True)
    if citations is not None:
        citations.extend(_extract_citations(result))
    action = _extract_pending_action(result) if actions else None
    if action is not None and pending_action is not None:
        pending_action.append(action)
    extracted_images = _extract_images(result) if images else []
    if generated_images is not None:
        generated_images.extend(extracted_images)
    extracted_code = _extract_code_results(result) if code_execution else []
    if code_results is not None:
        code_results.extend(extracted_code)

    notes = []
    if action is not None:
        notes.append(_action_confirmation_note(action))
    if extracted_images:
        notes.append(_image_generation_note(len(extracted_images)))
    if extracted_code:
        notes.append(_code_execution_note(len(extracted_code)))
    return _compose_answer_with_notes(_extract_text(result), notes)


def _stream_openai(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    web_search: bool = False,
    citations: list[Citation] | None = None,
    actions: bool = False,
    pending_action: list[PendingActionDict] | None = None,
    images: bool = False,
    generated_images: list[str] | None = None,
    attachments: list[str] | None = None,
    files: list[FileAttachment] | None = None,
    truncated: list[bool] | None = None,
    code_execution: bool = False,
    code_results: list[CodeResultDict] | None = None,
) -> Iterator[str]:
    """Yield output text deltas from a streaming Responses API call."""
    client = get_client().with_options(timeout=_timeout_seconds())
    attempts = _answer_attempts(
        reasoning_effort, web_search, actions, images, code_execution
    )

    stream = _create_with_fallback(
        client,
        model,
        question,
        max_output_tokens,
        attempts,
        stream=True,
        attachments=attachments,
        files=files,
    )

    yielded_any = False

    def _yield_action_note(response_obj: object) -> Iterator[str]:
        nonlocal yielded_any
        if not actions:
            return
        action = _extract_pending_action(response_obj)
        if action is None:
            return
        if pending_action is not None:
            pending_action.append(action)
        note = _action_confirmation_note(action)
        yield note if not yielded_any else f"\n\n{note}"
        yielded_any = True

    def _yield_image_note(response_obj: object) -> Iterator[str]:
        nonlocal yielded_any
        if not images:
            return
        extracted = _extract_images(response_obj)
        if not extracted:
            return
        if generated_images is not None:
            generated_images.extend(extracted)
        note = _image_generation_note(len(extracted))
        yield note if not yielded_any else f"\n\n{note}"
        yielded_any = True

    def _yield_code_note(response_obj: object) -> Iterator[str]:
        nonlocal yielded_any
        if not code_execution:
            return
        extracted = _extract_code_results(response_obj)
        if not extracted:
            return
        if code_results is not None:
            code_results.extend(extracted)
        note = _code_execution_note(len(extracted))
        yield note if not yielded_any else f"\n\n{note}"
        yielded_any = True

    for event in stream:  # type: ignore[attr-defined]
        event_type = getattr(event, "type", "")

        if event_type == "response.output_text.delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                yielded_any = True
                yield delta
        elif event_type == "response.completed":
            response_obj = getattr(event, "response", None)
            _record_openai_usage(response_obj, usage)
            if citations is not None:
                citations.extend(_extract_citations(response_obj))
            yield from _yield_image_note(response_obj)
            yield from _yield_code_note(response_obj)
            yield from _yield_action_note(response_obj)
        elif event_type == "response.incomplete":
            # A truncated response (usually reasoning consumed the whole token
            # budget) still reports usage. Record it so the call isn't billed as
            # $0, and log the reason — do NOT raise, so any partial text already
            # streamed is kept rather than discarded as a stream error.
            incomplete = getattr(event, "response", None)
            _record_openai_usage(incomplete, usage)
            if citations is not None:
                citations.extend(_extract_citations(incomplete))
            yield from _yield_image_note(incomplete)
            yield from _yield_code_note(incomplete)
            yield from _yield_action_note(incomplete)
            details = getattr(incomplete, "incomplete_details", None)
            reason = getattr(details, "reason", "") or "incomplete"
            logger.warning("stream.incomplete model=%s reason=%s", model, reason)
            if truncated is not None:
                truncated.append(True)
        elif event_type == "response.failed":
            response = getattr(event, "response", None)
            error = getattr(response, "error", None)
            message = getattr(error, "message", "") or "Model response failed."
            raise _ModelStreamError(message)
        elif event_type == "error":
            message = getattr(event, "message", "") or "Model stream error."
            raise _ModelStreamError(message)


def _call_model(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    web_search: bool = False,
    citations: list[Citation] | None = None,
    actions: bool = False,
    pending_action: list[PendingActionDict] | None = None,
    images: bool = False,
    generated_images: list[str] | None = None,
    attachments: list[str] | None = None,
    files: list[FileAttachment] | None = None,
    truncated: list[bool] | None = None,
    code_execution: bool = False,
    code_results: list[CodeResultDict] | None = None,
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
) -> str:
    """Dispatch a non-streaming call to the provider that owns the model.

    `web_search`/`citations`/`actions`/`pending_action`/`images`/
    `generated_images` only ever reach the native OpenAI path — none of these
    tools has an Anthropic/LiteLLM equivalent wired up here, and callers only
    ever set them True for an OpenAI-served model anyway (see
    routing._gate_live_data and orchestrator's actions/images gating), so this
    is a no-op for those providers by construction, not a silent gap.

    `attachments` (vision input images) and `files` (PDF/plain-text documents)
    are different: they're generic capabilities, not tools, so they're
    threaded to ALL THREE provider paths — a model that doesn't actually
    support one just errors or ignores it (drop_params, for LiteLLM), same as
    any other unsupported optional param.

    `cacheable_system` (see main.build_context_prompt_with_cache_split) only
    ever reaches Anthropic today — the one provider path here wired up with a
    native prompt-caching parameter (see providers._anthropic_system). OpenAI
    gets the same stable-prefix benefit automatically, for free, just by that
    text staying byte-identical inside `question` turn over turn; LiteLLM
    passes through none of the providers this app routes to it (Gemini,
    Bedrock, Mistral, Groq, Ollama) support Anthropic-style cache_control.

    `anthropic_question` is `question` with `cacheable_system`'s text already
    stripped off the front (see build_context_prompt_with_cache_split) —
    Anthropic uses THIS as the user turn whenever `cacheable_system` is set,
    instead of `question`, so that text isn't sent twice (once cached via
    `system`, once again at full price baked into the user turn).
    """
    provider = provider_of(model)
    if provider == "anthropic":
        effective_question = question
        if cacheable_system is not None and anthropic_question is not None:
            effective_question = anthropic_question
        return call_anthropic(
            model,
            effective_question,
            max_output_tokens,
            _timeout_seconds(),
            usage,
            attachments,
            files,
            truncated,
            cacheable_system,
        )
    if provider == "litellm":
        return call_litellm(
            model,
            question,
            max_output_tokens,
            _timeout_seconds(),
            reasoning_effort,
            usage,
            attachments,
            files,
            truncated,
        )
    return _call_openai(
        model,
        question,
        max_output_tokens,
        reasoning_effort,
        usage,
        web_search,
        citations,
        actions,
        pending_action,
        images,
        generated_images,
        attachments,
        files,
        truncated,
        code_execution,
        code_results,
    )


def _stream_model(
    model: str,
    question: str,
    max_output_tokens: int,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    web_search: bool = False,
    citations: list[Citation] | None = None,
    actions: bool = False,
    pending_action: list[PendingActionDict] | None = None,
    images: bool = False,
    generated_images: list[str] | None = None,
    attachments: list[str] | None = None,
    files: list[FileAttachment] | None = None,
    truncated: list[bool] | None = None,
    code_execution: bool = False,
    code_results: list[CodeResultDict] | None = None,
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
) -> Iterator[str]:
    """Dispatch a streaming call to the provider that owns the model. See
    _call_model's docstring for why the tool-only params are OpenAI-only,
    attachments/files are not, and what cacheable_system/anthropic_question
    are for."""
    provider = provider_of(model)
    if provider == "anthropic":
        effective_question = question
        if cacheable_system is not None and anthropic_question is not None:
            effective_question = anthropic_question
        yield from stream_anthropic(
            model,
            effective_question,
            max_output_tokens,
            _timeout_seconds(),
            usage,
            attachments,
            files,
            truncated,
            cacheable_system,
        )
        return
    if provider == "litellm":
        yield from stream_litellm(
            model,
            question,
            max_output_tokens,
            _timeout_seconds(),
            reasoning_effort,
            usage,
            attachments,
            files,
            truncated,
        )
        return
    yield from _stream_openai(
        model,
        question,
        max_output_tokens,
        reasoning_effort,
        usage,
        web_search,
        citations,
        actions,
        pending_action,
        images,
        generated_images,
        attachments,
        files,
        truncated,
        code_execution,
        code_results,
    )


def _apply_research_override(decision: RouteDecision, req: AskRequest) -> RouteDecision:
    """Research mode: force web_search on for this one request, regardless of
    the classifier's freshness judgment — for "look this up properly" asks
    the auto-mode heuristic might not flag as needing live data.

    Silently a no-op (same gating _gate_live_data already applies) unless
    WEB_SEARCH is enabled AND the resolved model is OpenAI-served, the only
    provider path that supports the tool — forcing it otherwise would just
    set a flag nothing downstream acts on.
    """
    if not req.research or decision.needs_live_data:
        return decision
    if not bool_setting("WEB_SEARCH", False) or provider_of(decision.model) != "openai":
        return decision
    return dataclasses.replace(
        decision,
        needs_live_data=True,
        notes=f"{decision.notes} | research mode: forced web search",
    )


def _auth_key_env(model: str) -> str:
    """The env var whose key an auth failure for this model implicates."""
    return key_env_for(model)


def _cache_key(req: AskRequest, owner: str | None = None) -> str | None:
    """The cache key for this request, or None when the cache should be skipped.

    Skipped entirely (no read AND no write) when:
    - caching is off;
    - a model is forced (the key doesn't encode it, so caching would read or
      poison the normally-routed entry);
    - no_cache is set (e.g. regenerate) — a one-off fresh answer must neither be
      served from nor written into the cache; or
    - the request has attached images or files — the key is question text
      only, so it can't distinguish "this question" from "this question +
      this photo/document", and the answer's correctness depends on the
      attachment's content, not just the text; or
    - research mode is on — forcing a live web search must never be served
      from (or overwrite) a cache entry answered without one.

    `owner` is folded into the key (see cache.make_key) so the cache is
    scoped per-user in a JWT multi-user deployment, not a cross-user oracle.
    """
    if (
        not cache.enabled()
        or req.model
        or req.no_cache
        or req.images
        or req.files
        or req.research
    ):
        return None
    return cache.make_key(req.question, req.mode.value, owner)


def _cached_hit_note(hit: dict, meta: object, ms: int) -> str:
    original = hit.get("mode_used") or "?"
    saved = hit.get("cost_usd")
    saved_note = (
        f", saved≈${saved:.4f}" if isinstance(saved, (int, float)) and saved else ""
    )
    return (
        f"Served from response cache (originally {original}{saved_note}) "
        f"| request_id={getattr(meta, 'request_id', '?')} | ms={ms}"
    )


def _cached_response(hit: dict, meta: object, ms: int) -> AskResponse:
    return AskResponse(
        answer=str(hit.get("answer") or ""),
        mode_used=str(hit.get("mode_used") or "cache"),
        notes=_cached_hit_note(hit, meta, ms),
        cost_usd=0.0,
        cached=True,
    )


def run_orchestrator(
    req: AskRequest,
    routing_question: str | None = None,
    owner: str | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
) -> AskResponse:
    """Route + answer a request.

    `routing_question` is the raw new user turn used ONLY for the routing
    decision (classifier / prefilter / heuristic); the model still answers on
    `req.question` (which may be a full conversation-context prompt). This keeps
    auto mode routing on the actual question instead of the assembled history —
    e.g. a code fence in an earlier turn must not force every later turn to the
    smart tier. Defaults to `req.question` (correct for the stateless endpoint,
    where the two are the same). `owner` is recorded against the call's spend.

    `history` is a short recent-turns snippet used only for the classifier's
    ambiguity check (see decide_route) — when it flags the new turn as
    referentially ambiguous, this returns a clarifying question as the whole
    answer instead of calling any fast/smart model.

    `cacheable_system` (see main.build_context_prompt_with_cache_split) is the
    stable system-prompt/history-summary prefix, threaded to _call_model for
    provider-native prompt caching — see _call_model's docstring.
    """
    meta = new_request_meta()
    route_question = routing_question or req.question

    key = _cache_key(req, owner)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            logger.info(
                "request.cache_hit id=%s ms=%s model=%s",
                meta.request_id,
                ms,
                hit.get("model"),
            )
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": str(hit.get("mode_used") or ""),
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                }
            )
            _record_avoided_cost(owner, hit, "response_cache_hit")
            return _cached_response(hit, meta, ms)

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("request.no_api_key id=%s", meta.request_id)
        return AskResponse(
            answer="",
            mode_used=str(req.mode.value),
            notes=f"{e} | request_id={meta.request_id}",
        )

    decision = decide_route(
        route_question, req.mode, client=client, forced_model=req.model, history=history
    )
    decision = _apply_research_override(decision, req)

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
        }
    )

    logger.info(
        "request.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    if decision.ambiguous:
        # No model call, no budget reservation, no cache write — the whole
        # point is that this is cheaper than guessing wrong and burning a
        # full answer on the wrong interpretation (see decide_route).
        ms = elapsed_ms(meta)
        logger.info("request.ambiguous id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer=decision.clarifying_question,
            mode_used=decision.mode_used,
            notes=f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
        )

    # Only OpenAI (Responses API) knows how to carry the propose_action /
    # image_generation / code_interpreter tools; other providers just never
    # see them offered (identical to web_search).
    actions_wanted = actions_enabled() and provider_of(decision.model) == "openai"
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider_of(decision.model) == "openai"
    )
    # The Gemini/Imagen image path is a standalone call, independent of which
    # model answers the question (unlike the OpenAI tool, which only the
    # resolved model itself can decide to invoke) — see _looks_like_image_request.
    gemini_image_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "gemini"
        and _looks_like_image_request(req.question)
    )
    code_execution_wanted = (
        _code_execution_enabled() and provider_of(decision.model) == "openai"
    )

    refusal, reservation_id = budget.reserve(
        decision.model,
        decision.max_output_tokens,
        req.question,
        _worst_case_image_cost(images_wanted, gemini_image_wanted),
        owner=owner,
    )
    if refusal is not None:
        ms = elapsed_ms(meta)
        logger.warning("request.budget_refused id=%s ms=%s", meta.request_id, ms)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"{refusal} | request_id={meta.request_id} | ms={ms}",
        )

    usage = Usage()
    citations: list[Citation] = []
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []

    try:
        answer_text = _call_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
            web_search=decision.needs_live_data,
            citations=citations,
            actions=actions_wanted,
            pending_action=pending_action,
            images=images_wanted,
            generated_images=generated_images,
            attachments=req.images,
            files=req.files,
            truncated=truncated,
            code_execution=code_execution_wanted,
            code_results=code_results,
            cacheable_system=cacheable_system,
            anthropic_question=anthropic_question,
        )

        if gemini_image_wanted:
            gemini_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if gemini_images:
                generated_images.extend(gemini_images)
                answer_text = _compose_answer_with_notes(
                    answer_text, [_image_generation_note(len(gemini_images))]
                )

        ms = elapsed_ms(meta)

        logger.info(
            "request.ok id=%s ms=%s model=%s tokens=%s",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
        )

        image_cost = (
            estimate_image_cost(len(generated_images), _image_generation_quality())
            if generated_images
            else None
        )
        code_execution_cost = estimate_code_execution_cost(len(code_results))
        extra_cost = (image_cost or 0.0) + (code_execution_cost or 0.0)
        response = AskResponse(
            answer=answer_text,
            mode_used=decision.mode_used,
            notes=f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
            sources=[Source(**c) for c in citations] or None,
            pending_action=(
                PendingAction.model_validate(pending_action[0])
                if pending_action
                else None
            ),
            images=generated_images or None,
            code_results=[CodeResult.model_validate(c) for c in code_results] or None,
            truncated=bool(truncated),
            **_usage_fields(decision.model, usage, extra_cost),
        )
        # A freshness-sensitive answer must not be frozen into the cache — a
        # later identical prompt would replay stale "current" info instead of
        # searching again. Only cache non-web-search answers. Same for a
        # proposed action, a generated image, or executed code: the cache has
        # no column to store any of them, so a cached hit would silently drop
        # them, and replaying a stale action proposal the client already
        # resolved would be actively wrong.
        if (
            key is not None
            and not decision.needs_live_data
            and not pending_action
            and not generated_images
            and not code_results
        ):
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_text,
                decision.mode_used,
                response.notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        _record_spend(owner, decision.model, usage, extra_cost, reservation_id)
        return response

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("request.auth_failed id=%s ms=%s", meta.request_id, ms)
        budget.release(reservation_id)
        return AskResponse(
            answer="",
            mode_used=decision.mode_used,
            notes=f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
        )

    except Exception as primary_error:
        # A rate-limit / quota error means the primary's key is throttled, so
        # only a DIFFERENT provider can help — fail over cross-vendor only.
        rate_limited = isinstance(primary_error, RATE_ERRORS)
        logger.exception(
            "request.primary_model_failed id=%s model=%s err=%s rate_limited=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
            rate_limited,
        )
        # The primary attempt is over (success or fail, no in-between): settle
        # its reservation now rather than carrying it into the fallback loop.
        _record_spend(owner, decision.model, usage, reservation_id=reservation_id)

        fallbacks = _fallback_models(decision.model, cross_provider_only=rate_limited)

        for fallback_model in fallbacks:
            # The pre-dispatch gate ran against the PRIMARY model, whose worst
            # case may have been $0 (a free local Ollama primary that turned
            # out to be down). Re-gate each fallback candidate so the failure
            # of a free model can't route PAID spend past an exhausted cap.
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model, decision.max_output_tokens, req.question, owner=owner
            )
            if fallback_refusal is not None:
                logger.warning(
                    "request.fallback_budget_refused id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )
                continue
            try:
                logger.info(
                    "request.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                fallback_usage = Usage()
                fallback_truncated: list[bool] = []
                answer_text = _call_model(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                    # Unlike web_search/actions/generated-images (OpenAI/Gemini-
                    # tool-specific, so a fallback provider might not support
                    # them at all), vision/file attachments are threaded to
                    # every provider path — dropping the user's image/document
                    # on fallback would silently lose context they explicitly
                    # provided.
                    attachments=req.images,
                    files=req.files,
                    truncated=fallback_truncated,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                )

                ms = elapsed_ms(meta)

                logger.info(
                    "request.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_response = AskResponse(
                    answer=answer_text,
                    mode_used=f"{decision.mode_used}->fallback",
                    notes=(
                        f"{decision.notes} | primary_model={decision.model} failed with "
                        f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                    truncated=bool(fallback_truncated),
                    **_usage_fields(fallback_model, fallback_usage),
                )
                # Same freshness invariant as the primary path: the fallback
                # never gets web_search/actions/images (documented scope
                # limit), so a live-data question answered by the fallback is
                # not search-grounded and must not be frozen into the cache
                # either.
                if (
                    key is not None
                    and not decision.needs_live_data
                    and not pending_action
                    and not generated_images
                ):
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        answer_text,
                        fallback_response.mode_used,
                        fallback_response.notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )
                return fallback_response

            except Exception as fallback_error:
                logger.exception(
                    "request.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )

        ms = elapsed_ms(meta)

        if rate_limited:
            notes = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            notes = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        return AskResponse(answer="", mode_used=decision.mode_used, notes=notes)


def stream_orchestrator(
    req: AskRequest,
    routing_question: str | None = None,
    owner: str | None = None,
    history: str = "",
    cacheable_system: str | None = None,
    anthropic_question: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Streaming variant of run_orchestrator.

    Yields plain dicts {"event": str, "data": dict} matching the SSE contract:
    one "meta" event, zero or more "delta" events, then a terminal "done" or
    "error" event. Persistence and wire formatting are the caller's job; this
    function never touches the message store (it does append spend-log rows).

    `routing_question` routes on the raw new user turn while the model answers
    on `req.question`; see run_orchestrator. `owner` is recorded against spend.
    `history` feeds the classifier's ambiguity check, same as run_orchestrator.
    `cacheable_system` is threaded to _stream_model for provider-native prompt
    caching; see run_orchestrator/_call_model's docstrings.
    """
    meta = new_request_meta()
    route_question = routing_question or req.question

    key = _cache_key(req, owner)
    if key is not None:
        hit = cache.get(key)
        if hit is not None:
            ms = elapsed_ms(meta)
            answer = str(hit.get("answer") or "")
            mode_used = str(hit.get("mode_used") or "cache")
            logger.info("stream.cache_hit id=%s ms=%s", meta.request_id, ms)
            enrich_span(
                **{
                    "ai.request_id": meta.request_id,
                    "ai.mode": req.mode.value,
                    "ai.mode_used": mode_used,
                    "ai.model": str(hit.get("model") or ""),
                    "ai.cache": "hit",
                    "ai.streaming": True,
                }
            )
            _record_avoided_cost(owner, hit, "response_cache_hit")
            yield {
                "event": "meta",
                "data": {
                    "request_id": meta.request_id,
                    "mode_used": mode_used,
                    "model": str(hit.get("model") or ""),
                    "notes": "cache=hit",
                },
            }
            if answer:
                yield {"event": "delta", "data": {"text": answer}}
            yield {
                "event": "done",
                "data": {
                    "answer": answer,
                    "mode_used": mode_used,
                    "notes": _cached_hit_note(hit, meta, ms),
                    "cached": True,
                },
            }
            return

    try:
        client = get_client()
    except RuntimeError as e:
        logger.error("stream.no_api_key id=%s", meta.request_id)
        yield {"event": "error", "data": {"message": str(e)}}
        return

    decision = decide_route(
        route_question, req.mode, client=client, forced_model=req.model, history=history
    )
    decision = _apply_research_override(decision, req)

    enrich_span(
        **{
            "ai.request_id": meta.request_id,
            "ai.mode": req.mode.value,
            "ai.mode_used": decision.mode_used,
            "ai.model": decision.model,
            "ai.provider": provider_of(decision.model),
            "ai.streaming": True,
        }
    )

    logger.info(
        "stream.start id=%s mode=%s routed=%s model=%s",
        meta.request_id,
        req.mode,
        decision.mode_used,
        decision.model,
    )

    if decision.ambiguous:
        # Same short-circuit as run_orchestrator, in SSE shape: one delta with
        # the whole clarifying question, then done — no model call at all.
        ms = elapsed_ms(meta)
        logger.info("stream.ambiguous id=%s ms=%s", meta.request_id, ms)
        yield {
            "event": "meta",
            "data": {
                "request_id": meta.request_id,
                "mode_used": decision.mode_used,
                "model": decision.model,
                "notes": decision.notes,
            },
        }
        yield {"event": "delta", "data": {"text": decision.clarifying_question}}
        yield {
            "event": "done",
            "data": {
                "answer": decision.clarifying_question,
                "mode_used": decision.mode_used,
                "notes": f"{decision.notes} | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    actions_wanted = actions_enabled() and provider_of(decision.model) == "openai"
    images_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "openai"
        and provider_of(decision.model) == "openai"
    )
    gemini_image_wanted = (
        _image_generation_enabled()
        and _image_generation_provider() == "gemini"
        and _looks_like_image_request(req.question)
    )
    code_execution_wanted = (
        _code_execution_enabled() and provider_of(decision.model) == "openai"
    )

    refusal, reservation_id = budget.reserve(
        decision.model,
        decision.max_output_tokens,
        req.question,
        _worst_case_image_cost(images_wanted, gemini_image_wanted),
        owner=owner,
    )
    if refusal is not None:
        ms = elapsed_ms(meta)
        logger.warning("stream.budget_refused id=%s ms=%s", meta.request_id, ms)
        # Refuse before any model work — no meta, matching the no-api-key path.
        yield {
            "event": "error",
            "data": {"message": f"{refusal} | request_id={meta.request_id} | ms={ms}"},
        }
        return

    yield {
        "event": "meta",
        "data": {
            "request_id": meta.request_id,
            "mode_used": decision.mode_used,
            "model": decision.model,
            "notes": decision.notes,
        },
    }

    streamed_any = False
    accumulated: list[str] = []
    usage = Usage()
    citations: list[Citation] = []
    pending_action: list[PendingActionDict] = []
    generated_images: list[str] = []
    truncated: list[bool] = []
    code_results: list[CodeResultDict] = []

    try:
        for text in _stream_model(
            model=decision.model,
            question=req.question,
            max_output_tokens=decision.max_output_tokens,
            reasoning_effort=decision.reasoning_effort,
            usage=usage,
            web_search=decision.needs_live_data,
            citations=citations,
            actions=actions_wanted,
            pending_action=pending_action,
            images=images_wanted,
            generated_images=generated_images,
            attachments=req.images,
            files=req.files,
            truncated=truncated,
            code_execution=code_execution_wanted,
            code_results=code_results,
            cacheable_system=cacheable_system,
            anthropic_question=anthropic_question,
        ):
            streamed_any = True
            accumulated.append(text)
            yield {"event": "delta", "data": {"text": text}}

        if gemini_image_wanted:
            gemini_images = generate_images_litellm(
                _image_generation_model(),
                req.question,
                _image_generation_quality(),
                _image_generation_size(),
            )
            if gemini_images:
                generated_images.extend(gemini_images)
                note = _image_generation_note(len(gemini_images))
                note_text = note if not accumulated else f"\n\n{note}"
                accumulated.append(note_text)
                streamed_any = True
                yield {"event": "delta", "data": {"text": note_text}}

        ms = elapsed_ms(meta)

        logger.info(
            "stream.ok id=%s ms=%s model=%s tokens=%s",
            meta.request_id,
            ms,
            decision.model,
            usage.total_tokens,
        )

        answer_final = "".join(accumulated).strip()
        done_notes = f"{decision.notes} | request_id={meta.request_id} | ms={ms}"
        image_cost = (
            estimate_image_cost(len(generated_images), _image_generation_quality())
            if generated_images
            else None
        )
        code_execution_cost = estimate_code_execution_cost(len(code_results))
        extra_cost = (image_cost or 0.0) + (code_execution_cost or 0.0)
        # See run_orchestrator: a freshness-sensitive answer, one with a
        # proposed action, a generated image, or executed code is never cached.
        if (
            key is not None
            and not decision.needs_live_data
            and not pending_action
            and not generated_images
            and not code_results
        ):
            cache.put(
                key,
                req.question,
                req.mode.value,
                answer_final,
                decision.mode_used,
                done_notes,
                decision.model,
                usage.input_tokens,
                usage.output_tokens,
                estimate_cost(decision.model, usage),
            )
        # Record spend even when answer_final is empty (truncated call): the
        # tokens were still billed, so the budget must see them.
        _record_spend(owner, decision.model, usage, extra_cost, reservation_id)

        yield {
            "event": "done",
            "data": {
                "answer": answer_final,
                "mode_used": decision.mode_used,
                "notes": done_notes,
                "truncated": bool(truncated),
                **({"sources": citations} if citations else {}),
                **({"pending_action": pending_action[0]} if pending_action else {}),
                **({"images": generated_images} if generated_images else {}),
                **({"code_results": code_results} if code_results else {}),
                **_usage_fields(decision.model, usage, extra_cost),
            },
        }
        return

    except GeneratorExit:
        # The client disconnected (Stop button, tab close, network drop)
        # mid-stream — Starlette closes this generator, which raises
        # GeneratorExit at the `yield` above. It's a BaseException, so the
        # `except Exception` below never sees it; without this clause the
        # provider had already billed these tokens but the budget never
        # would have found out. Record what was spent, then propagate the
        # close (a generator must never swallow GeneratorExit).
        if usage.total_tokens:
            logger.warning(
                "stream.client_disconnected id=%s model=%s tokens=%s",
                meta.request_id,
                decision.model,
                usage.total_tokens,
            )
            _record_spend(owner, decision.model, usage, reservation_id=reservation_id)
        else:
            budget.release(reservation_id)
        raise

    except AUTH_ERRORS:
        ms = elapsed_ms(meta)
        logger.exception("stream.auth_failed id=%s ms=%s", meta.request_id, ms)
        budget.release(reservation_id)
        yield {
            "event": "error",
            "data": {
                "message": f"Authentication failed. Check {_auth_key_env(decision.model)}. | request_id={meta.request_id} | ms={ms}",
            },
        }
        return

    except Exception as primary_error:
        # Rate-limit / quota: the same key stays throttled, so fail over to a
        # DIFFERENT provider only (if one is configured).
        rate_limited = isinstance(primary_error, RATE_ERRORS)
        # The primary attempt is over (success, partial stream, or clean
        # failure): settle its reservation either way before doing anything else.
        _record_spend(owner, decision.model, usage, reservation_id=reservation_id)
        if streamed_any:
            # Partial output already went out; no fallback is possible.
            ms = elapsed_ms(meta)
            logger.exception(
                "stream.interrupted id=%s ms=%s model=%s err=%s",
                meta.request_id,
                ms,
                decision.model,
                type(primary_error).__name__,
            )
            yield {
                "event": "error",
                "data": {
                    "message": (
                        f"Stream interrupted: {type(primary_error).__name__}: {primary_error} "
                        f"| request_id={meta.request_id} | ms={ms}"
                    ),
                },
            }
            return

        logger.exception(
            "stream.primary_model_failed id=%s model=%s err=%s",
            meta.request_id,
            decision.model,
            type(primary_error).__name__,
        )

        for fallback_model in _fallback_models(
            decision.model, cross_provider_only=rate_limited
        ):
            # Same re-gate as run_orchestrator's fallback loop: the primary's
            # pre-dispatch check may have priced at $0 (free local model), so
            # each paid candidate must clear the budget itself.
            fallback_refusal, fallback_reservation_id = budget.reserve(
                fallback_model, decision.max_output_tokens, req.question, owner=owner
            )
            if fallback_refusal is not None:
                logger.warning(
                    "stream.fallback_budget_refused id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )
                continue
            fallback_parts: list[str] = []
            fallback_usage = Usage()
            fallback_truncated: list[bool] = []

            try:
                logger.info(
                    "stream.fallback_try id=%s fallback_model=%s",
                    meta.request_id,
                    fallback_model,
                )

                for text in _stream_model(
                    model=fallback_model,
                    question=req.question,
                    max_output_tokens=decision.max_output_tokens,
                    reasoning_effort=decision.reasoning_effort,
                    usage=fallback_usage,
                    # See run_orchestrator's fallback call: vision/file
                    # attachments work across every provider, unlike the
                    # OpenAI/Gemini-tool extras, so they're kept on fallback too.
                    attachments=req.images,
                    files=req.files,
                    truncated=fallback_truncated,
                    cacheable_system=cacheable_system,
                    anthropic_question=anthropic_question,
                ):
                    fallback_parts.append(text)
                    yield {"event": "delta", "data": {"text": text}}

                ms = elapsed_ms(meta)

                logger.info(
                    "stream.fallback_ok id=%s ms=%s fallback_model=%s",
                    meta.request_id,
                    ms,
                    fallback_model,
                )

                fallback_answer = "".join(fallback_parts).strip()
                fallback_notes = (
                    f"{decision.notes} | primary_model={decision.model} failed with "
                    f"{type(primary_error).__name__} | fallback_model={fallback_model} succeeded "
                    f"| request_id={meta.request_id} | ms={ms}"
                )
                # See run_orchestrator: the fallback never gets web_search,
                # actions, or images, so a live-data question answered by it
                # must not be cached either.
                if (
                    key is not None
                    and not decision.needs_live_data
                    and not pending_action
                    and not generated_images
                ):
                    cache.put(
                        key,
                        req.question,
                        req.mode.value,
                        fallback_answer,
                        f"{decision.mode_used}->fallback",
                        fallback_notes,
                        fallback_model,
                        fallback_usage.input_tokens,
                        fallback_usage.output_tokens,
                        estimate_cost(fallback_model, fallback_usage),
                    )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )

                yield {
                    "event": "done",
                    "data": {
                        "answer": fallback_answer,
                        "mode_used": f"{decision.mode_used}->fallback",
                        "notes": fallback_notes,
                        "truncated": bool(fallback_truncated),
                        **_usage_fields(fallback_model, fallback_usage),
                    },
                }
                return

            except GeneratorExit:
                # Same client-disconnect case as the primary stream's handler
                # above, but for a fallback candidate's own stream.
                if fallback_usage.total_tokens:
                    logger.warning(
                        "stream.client_disconnected id=%s fallback_model=%s tokens=%s",
                        meta.request_id,
                        fallback_model,
                        fallback_usage.total_tokens,
                    )
                    _record_spend(
                        owner,
                        fallback_model,
                        fallback_usage,
                        reservation_id=fallback_reservation_id,
                    )
                else:
                    budget.release(fallback_reservation_id)
                raise

            except Exception as fallback_error:
                logger.exception(
                    "stream.fallback_failed id=%s fallback_model=%s err=%s",
                    meta.request_id,
                    fallback_model,
                    type(fallback_error).__name__,
                )
                _record_spend(
                    owner,
                    fallback_model,
                    fallback_usage,
                    reservation_id=fallback_reservation_id,
                )

                if fallback_parts:
                    # This fallback streamed partial output; stop entirely.
                    ms = elapsed_ms(meta)
                    yield {
                        "event": "error",
                        "data": {
                            "message": (
                                f"Stream interrupted: {type(fallback_error).__name__}: {fallback_error} "
                                f"| request_id={meta.request_id} | ms={ms}"
                            ),
                        },
                    }
                    return

        ms = elapsed_ms(meta)

        if rate_limited:
            message = (
                f"Rate limited / quota exceeded; no cross-vendor fallback "
                f"available. primary_model={decision.model} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        else:
            message = (
                f"Primary model failed and no fallback succeeded. "
                f"primary_model={decision.model} | err={type(primary_error).__name__}: {primary_error} "
                f"| request_id={meta.request_id} | ms={ms}"
            )
        yield {"event": "error", "data": {"message": message}}
        return
