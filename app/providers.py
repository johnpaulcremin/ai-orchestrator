from __future__ import annotations

import os
from collections.abc import Iterator

import anthropic
from openai import AuthenticationError, RateLimitError

# Unified error tuples so the orchestrator handles auth/rate failures the same
# way regardless of which provider raised them. Anthropic's exception classes
# mirror OpenAI's, but they are distinct types, so both must be listed. Any other
# error triggers the orchestrator's fallback chain.
AUTH_ERRORS = (AuthenticationError, anthropic.AuthenticationError)
RATE_ERRORS = (RateLimitError, anthropic.RateLimitError)


def provider_of(model: str) -> str:
    """Which provider a model name belongs to. Anthropic if it looks like Claude."""
    name = (model or "").strip().lower()
    if name.startswith("claude") or name.startswith("anthropic/"):
        return "anthropic"
    return "openai"


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


def call_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
) -> str:
    """Non-streaming Claude call via the Messages API. Reasoning effort is an
    OpenAI-tier concept and does not apply here."""
    client = anthropic_client(timeout)
    message = client.messages.create(
        model=_anthropic_model(model),
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": question}],
    )
    parts = [
        block.text
        for block in message.content
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


def stream_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
) -> Iterator[str]:
    """Streaming Claude call: yields text deltas from the Messages API."""
    client = anthropic_client(timeout)
    with client.messages.stream(
        model=_anthropic_model(model),
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        for text in stream.text_stream:
            if text:
                yield text
