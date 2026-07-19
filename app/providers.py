from __future__ import annotations

import os
from collections.abc import Iterator

import anthropic
from openai import AuthenticationError, RateLimitError

from .usage import Usage

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


def call_anthropic(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    usage: Usage | None = None,
) -> str:
    """Non-streaming Claude call via the Messages API. Reasoning effort is an
    OpenAI-tier concept and does not apply here."""
    client = anthropic_client(timeout)
    message = client.messages.create(
        model=_anthropic_model(model),
        max_tokens=max_output_tokens,
        messages=[{"role": "user", "content": question}],
    )
    _record(usage, getattr(message, "usage", None), "input_tokens", "output_tokens")
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
    usage: Usage | None = None,
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


def _litellm_kwargs(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str,
) -> dict:
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "max_tokens": max_output_tokens,
        "timeout": timeout,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def call_litellm(
    model: str,
    question: str,
    max_output_tokens: int,
    timeout: float,
    reasoning_effort: str = "",
    usage: Usage | None = None,
) -> str:
    """Non-streaming call to any LiteLLM-supported provider (Gemini, Bedrock,
    Mistral, ...). Credentials come from that provider's standard env vars."""
    litellm = _litellm()
    response = litellm.completion(
        **_litellm_kwargs(model, question, max_output_tokens, timeout, reasoning_effort)
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
) -> Iterator[str]:
    """Streaming call via LiteLLM: yields text deltas."""
    litellm = _litellm()
    stream = litellm.completion(
        stream=True,
        stream_options={"include_usage": True},
        **_litellm_kwargs(
            model, question, max_output_tokens, timeout, reasoning_effort
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
