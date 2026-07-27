from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass


@dataclass
class Usage:
    """Token counts for a single model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    # How many of `input_tokens` were served from the provider's prompt cache
    # (OpenAI reports this as usage.input_tokens_details.cached_tokens). These
    # are billed at a discount. Non-OpenAI providers report 0, so their cost is
    # unchanged.
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# Approximate list price in USD per 1,000,000 tokens, as
# (input, output) or (input, output, cached_input). These change often — treat
# them as estimates and override via MODEL_PRICING (a JSON object of
# {"model": [input, output]} or {"model": [input, output, cached_input]}) for
# exact figures. Models not listed report tokens but no cost. When a model has
# no cached-input rate, cached tokens are billed at input * CACHED_INPUT_MULTIPLIER.
_DEFAULT_PRICING: dict[str, tuple[float, ...]] = {
    "gpt-5": (1.25, 10.0, 0.125),
    "gpt-5-mini": (0.25, 2.0, 0.025),
    "gpt-5-nano": (0.05, 0.40, 0.005),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0),
    "gemini/gemini-flash-latest": (0.30, 2.50),
    "gemini/gemini-flash-lite-latest": (0.10, 0.40),
    "gemini/gemini-2.0-flash": (0.10, 0.40),
    "groq/llama-3.3-70b-versatile": (0.59, 0.79),
}

# Default discount for cached input tokens when a model has no explicit cached
# rate: OpenAI's cached input is roughly a tenth of its normal input price.
_DEFAULT_CACHED_INPUT_MULTIPLIER = 0.1

# Approximate per-image USD cost for the image_generation tool, by quality —
# this isn't token-based, so it can't come from _DEFAULT_PRICING. Treat these
# as rough estimates (1024x1024-class); override the per-image price with
# IMAGE_GENERATION_COST_USD for exact figures.
_DEFAULT_IMAGE_GENERATION_COST_USD: dict[str, float] = {
    "low": 0.02,
    "medium": 0.07,
    "high": 0.19,
    "auto": 0.07,
}

# Approximate flat per-call USD cost for the code_interpreter tool's sandboxed
# container session — not token-based, so it can't come from _DEFAULT_PRICING.
# Treat this as a rough estimate; override with CODE_EXECUTION_COST_USD for
# exact figures.
_DEFAULT_CODE_EXECUTION_COST_USD = 0.03

# /v1/speak (TTS) and /v1/transcribe bill per character of input / per minute
# of audio respectively, not per LLM token, so neither fits _DEFAULT_PRICING.
# These are rough flat estimates so the daily budget cap can still bound them;
# override via SPEECH_COST_PER_1K_CHARS_USD / TRANSCRIPTION_COST_PER_CALL_USD
# for exact figures.
_DEFAULT_SPEECH_COST_PER_1K_CHARS_USD = 0.015
_DEFAULT_TRANSCRIPTION_COST_PER_CALL_USD = 0.006


def _cached_input_multiplier() -> float:
    raw = (os.getenv("CACHED_INPUT_MULTIPLIER") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_CACHED_INPUT_MULTIPLIER
    except ValueError:
        return _DEFAULT_CACHED_INPUT_MULTIPLIER
    # Reject nan/inf: ordered comparisons against NaN are all False, so the clamp
    # below would let NaN through and poison every cost estimate.
    if not math.isfinite(value):
        return _DEFAULT_CACHED_INPUT_MULTIPLIER
    # A discount, so clamp to a sane [0, 1].
    return min(max(value, 0.0), 1.0)


def _pricing() -> dict[str, tuple[float, ...]]:
    table = dict(_DEFAULT_PRICING)
    raw = (os.getenv("MODEL_PRICING") or "").strip()
    if raw:
        try:
            for model, values in json.loads(raw).items():
                rates = tuple(float(v) for v in values[:3])
                if len(rates) >= 2:
                    table[model] = rates
        except (ValueError, TypeError, KeyError, IndexError):
            pass
    return table


def estimate_cost(model: str, usage: Usage | None) -> float | None:
    """Estimated USD cost for a call, or None if the model isn't priced.

    Cached input tokens are billed at the model's cached-input rate (a 3rd price
    value) if given, else at input_rate * CACHED_INPUT_MULTIPLIER. With
    cached_input_tokens == 0 the result is identical to input+output pricing.
    """
    if usage is None:
        return None
    table = _pricing()
    price = table.get(model)
    if price is None:
        # Fall back to a bare name (drop the outer provider prefix), e.g.
        # "gemini/gemini-flash-lite-latest" -> "gemini-flash-lite-latest".
        bare = model.split("/", 1)[-1]
        price = table.get(bare)
    if price is None and "/" in model:
        # A double-prefixed name — e.g. OpenRouter's own
        # "openrouter/<vendor>/<model>" convention (LiteLLM routes OpenRouter
        # models with the vendor namespaced inside the path) — needs a second
        # strip to reach the truly bare model id; the first strip above only
        # gets to "<vendor>/<model>", which still won't match a plain entry.
        last_segment = model.rsplit("/", 1)[-1]
        price = table.get(last_segment)
    if price is None:
        # Local Ollama inference has no per-token price — it's genuinely $0,
        # not "unpriced". Reporting 0.0 (rather than None) means the UI shows
        # $0 instead of nothing and the daily-budget gate can bound the call
        # instead of warning about an unpriceable model. Both table lookups
        # run first, so an explicit MODEL_PRICING entry (full or bare name)
        # still wins for anyone who wants to account for local compute.
        # "*-cloud" tags are excluded: the local daemon transparently proxies
        # those to Ollama's usage-metered PAID cloud, so they stay unpriced
        # (and loudly warned about) rather than silently booked as free.
        name = model.strip().lower()
        if name.startswith(("ollama/", "ollama_chat/")) and not name.endswith("-cloud"):
            return 0.0
        return None

    input_rate, output_rate = price[0], price[1]
    cached_rate = (
        price[2] if len(price) > 2 else input_rate * _cached_input_multiplier()
    )

    # cached_input_tokens is a subset of input_tokens; guard against bad data.
    cached = max(0, min(usage.cached_input_tokens, usage.input_tokens))
    uncached = usage.input_tokens - cached

    cost = (
        uncached / 1_000_000 * input_rate
        + cached / 1_000_000 * cached_rate
        + usage.output_tokens / 1_000_000 * output_rate
    )
    # A non-finite price (e.g. a NaN/inf slipped into MODEL_PRICING) must not
    # corrupt the total — report unpriced rather than NaN.
    return cost if math.isfinite(cost) else None


def estimate_image_cost(count: int, quality: str) -> float | None:
    """Estimated USD cost for `count` generated images at the given quality.

    None (not 0.0) when count is 0, so callers can tell "no images" apart from
    "images that happen to cost nothing" without a separate check.
    """
    if count <= 0:
        return None
    raw = (os.getenv("IMAGE_GENERATION_COST_USD") or "").strip()
    per_image = _DEFAULT_IMAGE_GENERATION_COST_USD.get(
        quality, _DEFAULT_IMAGE_GENERATION_COST_USD["auto"]
    )
    if raw:
        try:
            value = float(raw)
            if math.isfinite(value) and value >= 0:
                per_image = value
        except ValueError:
            pass
    return count * per_image


def estimate_code_execution_cost(count: int) -> float | None:
    """Estimated USD cost for `count` code_interpreter tool calls.

    None (not 0.0) when count is 0, so callers can tell "no code ran" apart
    from "code that happened to cost nothing" without a separate check.
    """
    if count <= 0:
        return None
    return count * _positive_float_env(
        "CODE_EXECUTION_COST_USD", _DEFAULT_CODE_EXECUTION_COST_USD
    )


def _positive_float_env(env_var: str, default: float) -> float:
    raw = (os.getenv(env_var) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def estimate_speech_cost(text: str) -> float:
    """Rough USD cost estimate for one /v1/speak call, priced per 1K input
    characters (OpenAI TTS bills by character, not by LLM token)."""
    rate = _positive_float_env(
        "SPEECH_COST_PER_1K_CHARS_USD", _DEFAULT_SPEECH_COST_PER_1K_CHARS_USD
    )
    return len(text or "") / 1000 * rate


def estimate_transcription_cost() -> float:
    """Rough flat USD cost estimate for one /v1/transcribe call.

    Whisper-class transcription bills per minute of audio, which isn't known
    before decoding the clip; rather than decode audio just to price it, this
    uses a flat per-call estimate sized for a short mic-button dictation clip.
    """
    return _positive_float_env(
        "TRANSCRIPTION_COST_PER_CALL_USD", _DEFAULT_TRANSCRIPTION_COST_PER_CALL_USD
    )
