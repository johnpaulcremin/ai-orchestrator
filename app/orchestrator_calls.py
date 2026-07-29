"""The provider-dispatch call chain: the lazily-created OpenAI client, the
retry/fallback ladder for a single OpenAI Responses API call
(_create_with_fallback/_answer_attempts), the OpenAI call/stream functions
built on top of it, and _call_model/_stream_model, which route a request to
whichever of OpenAI/Anthropic/LiteLLM actually owns the model. Kept as one
file because these functions call each other by bare name — see
orchestrator.py's module docstring-equivalent notes in the split's commit
for why that matters for tests that monkeypatch them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from openai import BadRequestError, OpenAI

from .orchestrator_extract import (
    Citation,
    CodeResultDict,
    _action_confirmation_note,
    _compose_answer_with_notes,
    _extract_citations,
    _extract_code_results,
    _extract_images,
    _extract_pending_action,
    _extract_text,
    _code_execution_note,
    _image_generation_note,
    PendingActionDict,
    _record_openai_usage,
)
from .orchestrator_tools import _build_tools
from .providers import (
    call_anthropic,
    call_litellm,
    key_env_for,
    provider_of,
    stream_anthropic,
    stream_litellm,
)
from .schemas import FileAttachment
from .settings import model_setting
from .telemetry import logger
from .usage import Usage

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


def _auth_key_env(model: str) -> str:
    """The env var whose key an auth failure for this model implicates."""
    return key_env_for(model)


class _ModelStreamError(Exception):
    """Raised when the streaming API reports a terminal failure event."""


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

    `web_search`/`citations`, `actions`/`pending_action`, and
    `code_execution`/`code_results` all reach both the OpenAI and Anthropic
    paths — each has its own native tool for all three (see
    providers.call_anthropic's `_ANTHROPIC_WEB_SEARCH_TOOL`/
    `_anthropic_action_tool`/`_ANTHROPIC_CODE_EXECUTION_TOOL`). Claude's
    tool-only responses carry no text for any of these, so `_call_model`
    composes the same confirmation note OpenAI's own `_call_openai` already
    does. `images`/`generated_images` only ever reach OpenAI: image_generation
    has no Anthropic/LiteLLM equivalent wired up here, and callers only ever
    set it True for an OpenAI-served model anyway (see orchestrator's images
    gating), so this is a no-op for those providers by construction, not a
    silent gap. LiteLLM gets none of these tools — no hosted-tool support
    wired up for any of the providers this app routes through it (Gemini,
    Bedrock, Mistral, Groq, Ollama).

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
        anthropic_action: list[PendingActionDict] = []
        anthropic_code: list[CodeResultDict] = []
        text = call_anthropic(
            model,
            effective_question,
            max_output_tokens,
            _timeout_seconds(),
            usage,
            attachments,
            files,
            truncated,
            cacheable_system,
            web_search,
            citations,
            actions,
            anthropic_action,
            code_execution,
            anthropic_code,
        )
        notes = []
        if anthropic_action:
            action = anthropic_action[0]
            if pending_action is not None:
                pending_action.append(action)
            notes.append(_action_confirmation_note(action))
        if anthropic_code:
            if code_results is not None:
                code_results.extend(anthropic_code)
            notes.append(_code_execution_note(len(anthropic_code)))
        return _compose_answer_with_notes(text, notes)
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
    _call_model's docstring for which tool-only params reach which providers,
    what attachments/files' every-provider treatment is, and what
    cacheable_system/anthropic_question are for."""
    provider = provider_of(model)
    if provider == "anthropic":
        effective_question = question
        if cacheable_system is not None and anthropic_question is not None:
            effective_question = anthropic_question
        anthropic_action: list[PendingActionDict] = []
        anthropic_code: list[CodeResultDict] = []
        yielded_any = False
        for chunk in stream_anthropic(
            model,
            effective_question,
            max_output_tokens,
            _timeout_seconds(),
            usage,
            attachments,
            files,
            truncated,
            cacheable_system,
            web_search,
            citations,
            actions,
            anthropic_action,
            code_execution,
            anthropic_code,
        ):
            yielded_any = True
            yield chunk
        if anthropic_code:
            if code_results is not None:
                code_results.extend(anthropic_code)
            note = _code_execution_note(len(anthropic_code))
            yield note if not yielded_any else f"\n\n{note}"
            yielded_any = True
        if anthropic_action:
            action = anthropic_action[0]
            if pending_action is not None:
                pending_action.append(action)
            note = _action_confirmation_note(action)
            yield note if not yielded_any else f"\n\n{note}"
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
