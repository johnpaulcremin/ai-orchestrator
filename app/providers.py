from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Iterator, Sequence
from typing import Protocol

import anthropic
from openai import AuthenticationError, RateLimitError

from .telemetry import logger
from .usage import Usage


class FileLike(Protocol):
    """Structural type for a document attachment (schemas.FileAttachment).

    Duck-typed rather than importing schemas.FileAttachment directly: settings
    -> providers already, and schemas -> settings, so providers -> schemas
    would be a circular import.
    """

    filename: str
    data: str


# Unified error tuples so the orchestrator handles auth/rate failures the same
# way regardless of which provider raised them. Anthropic's exception classes
# mirror OpenAI's, but they are distinct types, so both must be listed. Any other
# error triggers the orchestrator's fallback chain.
AUTH_ERRORS = (AuthenticationError, anthropic.AuthenticationError)
RATE_ERRORS = (RateLimitError, anthropic.RateLimitError)


def provider_of(model: str) -> str:
    """
    Which code path handles a model:

    - "anthropic": native Anthropic Messages API (names starting with "claude"
      or "anthropic/").
    - "litellm": any provider-prefixed name (e.g. "gemini/...", "bedrock/...",
      "mistral/...", "groq/...") — routed through LiteLLM.
    - "openai": everything else (bare names like "gpt-5") via the native
      OpenAI Responses API.
    """
    name = (model or "").strip().lower()
    if name.startswith("claude") or name.startswith("anthropic/"):
        return "anthropic"
    if "/" in name:
        return "litellm"
    return "openai"


# Env var an auth failure implicates, per LiteLLM provider prefix. Falls back to
# a generic phrase for prefixes not listed here.
_LITELLM_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "Vertex AI credentials",
    "bedrock": "AWS credentials",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "groq": "GROQ_API_KEY",
    "together_ai": "TOGETHER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    # Local inference — no API key exists; a failure here means the Ollama
    # server isn't reachable, not that a credential is wrong. "ollama_chat/"
    # is LiteLLM's chat-API variant of the same server.
    "ollama": "the local Ollama server (no API key needed — is it running?)",
    "ollama_chat": "the local Ollama server (no API key needed — is it running?)",
}


def key_env_for(model: str) -> str:
    """The credential an auth failure for this model points at."""
    provider = provider_of(model)
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider == "openai":
        return "OPENAI_API_KEY"
    prefix = model.split("/", 1)[0].strip().lower()
    return _LITELLM_KEY_ENV.get(prefix, f"the {prefix} credentials")


_anthropic_client: anthropic.Anthropic | None = None


def anthropic_client(timeout: float) -> anthropic.Anthropic:
    """Lazily create the Anthropic client so the module imports without a key."""
    global _anthropic_client

    if _anthropic_client is None:
        api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set but a Claude model was requested. "
                "Check your .env and shell env vars."
            )
        _anthropic_client = anthropic.Anthropic(api_key=api_key)

    return _anthropic_client.with_options(timeout=timeout)


def _anthropic_model(model: str) -> str:
    """Strip an optional 'anthropic/' prefix; the SDK wants the bare model id."""
    name = model.strip()
    return name[len("anthropic/") :] if name.lower().startswith("anthropic/") else name


def _record(usage: Usage | None, source: object, in_attr: str, out_attr: str) -> None:
    if usage is None or source is None:
        return
    usage.input_tokens = int(getattr(source, in_attr, 0) or 0)
    usage.output_tokens = int(getattr(source, out_attr, 0) or 0)


def _parse_data_url(url: str) -> tuple[str, str] | None:
    """Split a `data:<mime>;base64,<data>` URL into (mime, base64_data).

    None for anything else — callers silently skip a malformed attachment
    rather than sending it on to a provider API as a broken image block.
    """
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, b64 = url.split(",", 1)
    mime = header[len("data:") :].split(";", 1)[0] or "image/png"
    return mime, b64


def _anthropic_document_block(file: FileLike) -> dict[str, object] | None:
    """A Claude `document` content block for a PDF or plain-text attachment.

    None for anything unparseable or an unsupported mime — skipped rather than
    sent on as a broken block. PDFs stay base64; Claude's plain-text source
    wants the RAW decoded text, not base64 (unlike every other content type
    here), so text/plain is decoded before being sent.
    """
    parsed = _parse_data_url(file.data)
    if parsed is None:
        return None
    mime, b64 = parsed
    if mime == "application/pdf":
        source: dict[str, object] = {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        }
    elif mime == "text/plain":
        try:
            text = base64.b64decode(b64).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None
        source = {"type": "text", "media_type": "text/plain", "data": text}
    else:
        return None
    return {"type": "document", "source": source, "title": file.filename}


def _anthropic_content(
    question: str,
    attachments: list[str] | None,
    files: Sequence[FileLike] | None = None,
) -> str | list[dict[str, object]]:
    """Claude Messages API content: plain text, or a text + image/document
    block list when the user attached vision input and/or files."""
    if not attachments and not files:
        return question
    content: list[dict[str, object]] = [{"type": "text", "text": question}]
    for url in attachments or []:
        parsed = _parse_data_url(url)
        if parsed is None:
            continue
        mime, b64 = parsed
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        )
    for file in files or []:
        block = _anthropic_document_block(file)
        if block is not None:
            content.append(block)
    return content


def call_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> str:
    """Non-streaming Claude call via the Messages API. Reasoning effort is an
    OpenAI-tier concept and does not apply here."""
    client = anthropic_client(timeout)
    message = client.messages.create(
        model=_anthropic_model(model),
        max_tokens=max_output_tokens,
        messages=[
            {
                "role": "user",
                "content": _anthropic_content(question, attachments, files),  # type: ignore[typeddict-item]
            }
        ],
    )
    _record(usage, getattr(message, "usage", None), "input_tokens", "output_tokens")
    parts = [
        getattr(block, "text", "")
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def stream_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> Iterator[str]:
    """Streaming Claude call: yields text deltas from the Messages API."""
    client = anthropic_client(timeout)
    with client.messages.stream(
        model=_anthropic_model(model),
        max_tokens=max_output_tokens,
        messages=[
            {
                "role": "user",
                "content": _anthropic_content(question, attachments, files),  # type: ignore[typeddict-item]
            }
        ],
    ) as stream:
        for text in stream.text_stream:
            if text:
                yield text
        if usage is not None:
            final = stream.get_final_message()
            _record(
                usage, getattr(final, "usage", None), "input_tokens", "output_tokens"
            )


_litellm_mod = None


def _litellm():
    """Import and configure LiteLLM lazily (its import is heavy)."""
    global _litellm_mod
    if _litellm_mod is None:
        import litellm

        # Drop params a given provider doesn't support (e.g. reasoning_effort)
        # instead of erroring; keep it quiet.
        litellm.drop_params = True
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        _litellm_mod = litellm
    return _litellm_mod


def _litellm_content(
    question: str,
    attachments: list[str] | None,
    files: Sequence[FileLike] | None = None,
) -> str | list[dict[str, object]]:
    """LiteLLM's OpenAI-compatible content shape: plain text, or a text +
    image/file block list when the user attached vision input and/or files.
    LiteLLM normalizes `image_url`/`file` blocks across providers (Gemini,
    Bedrock, ...) that support them; drop_params (see _litellm()) drops
    whichever a given provider doesn't support, rather than erroring."""
    if not attachments and not files:
        return question
    content: list[dict[str, object]] = [{"type": "text", "text": question}]
    content.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in attachments or []
    )
    content.extend(
        {"type": "file", "file": {"filename": file.filename, "file_data": file.data}}
        for file in files or []
    )
    return content


def _litellm_kwargs(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> dict:
    kwargs = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": _litellm_content(question, attachments, files),
            }
        ],
        "max_tokens": max_output_tokens,
        "timeout": timeout,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def generate_images_litellm(
    model: str, prompt: str, quality: str, size: str, n: int = 1
) -> list[str]:
    """Generate images via any LiteLLM-supported image provider (currently:
    Gemini/Imagen, model="gemini/imagen-...", credentials from GEMINI_API_KEY).

    Returns ready-to-render `data:image/png;base64,...` URLs. Never raises — an
    image is an enrichment on top of the normal text answer, not worth failing
    the whole request over (mirrors the OpenAI tool path's _extract_images).
    litellm.drop_params (set in _litellm()) silently drops any of
    quality/size the target provider doesn't support, rather than erroring.
    """
    litellm = _litellm()
    try:
        response = litellm.image_generation(
            model=model,
            prompt=prompt,
            n=n,
            quality=quality,
            size=size,
            response_format="b64_json",
        )
    except Exception:
        logger.exception("images.litellm_generate_failed model=%s", model)
        return []

    images: list[str] = []
    for item in getattr(response, "data", None) or []:
        b64 = getattr(item, "b64_json", None)
        if b64:
            images.append(f"data:image/png;base64,{b64}")
    return images


def call_litellm(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> str:
    """Non-streaming call to any LiteLLM-supported provider (Gemini, Bedrock,
    Mistral, ...). Credentials come from that provider's standard env vars."""
    litellm = _litellm()
    response = litellm.completion(
        **_litellm_kwargs(
            model,
            question,
            max_output_tokens,
            timeout,
            reasoning_effort,
            attachments,
            files,
        )
    )
    _record(
        usage, getattr(response, "usage", None), "prompt_tokens", "completion_tokens"
    )
    content = response.choices[0].message.content
    return (content or "").strip()


def stream_litellm(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str = "",
    usage: Usage | None = None,
    attachments: list[str] | None = None,
    files: Sequence[FileLike] | None = None,
) -> Iterator[str]:
    """Streaming call via LiteLLM: yields text deltas."""
    litellm = _litellm()
    stream = litellm.completion(
        stream=True,
        stream_options={"include_usage": True},
        **_litellm_kwargs(
            model,
            question,
            max_output_tokens,
            timeout,
            reasoning_effort,
            attachments,
            files,
        ),
    )
    for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if choices:
            delta = getattr(choices[0].delta, "content", None) or ""
            if delta:
                yield delta
        # The final chunk (include_usage) carries usage with empty choices.
        if usage is not None and getattr(chunk, "usage", None):
            _record(usage, chunk.usage, "prompt_tokens", "completion_tokens")
