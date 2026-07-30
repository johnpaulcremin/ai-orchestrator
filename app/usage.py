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
    # (OpenAI: usage.input_tokens_details.cached_tokens; Anthropic:
    # cache_read_input_tokens, folded in by providers._record_anthropic). These
    # are billed at a discount. A provider that reports neither leaves this 0,
    # so its cost is unchanged.
    cached_input_tokens: int = 0
    # How many of `input_tokens` were newly WRITTEN to the provider's prompt
    # cache this call (Anthropic's cache_creation_input_tokens — no OpenAI
    # equivalent, since its caching is fully automatic with no separate write
    # step). Billed at a PREMIUM over normal input, not a discount — the
    # opposite direction from cached_input_tokens — since establishing a cache
    # entry costs the provider extra work. 0 for any provider that doesn't
    # report a cache-write count.
    cache_write_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# Approximate list price in USD per 1,000,000 tokens, as (input, output),
# (input, output, cached_input), or (input, output, cached_input,
# cache_write_input). These change often — treat them as estimates and
# override via MODEL_PRICING (a JSON object of {"model": [input, output]} /
# [..., cached_input] / [..., cached_input, cache_write_input]) for exact
# figures. Models not listed report tokens but no cost. When a model has no
# cached-input rate, cached tokens are billed at input * CACHED_INPUT_MULTIPLIER;
# when it has no cache-write rate, cache-write tokens are billed at
# input * CACHE_WRITE_MULTIPLIER.
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

# Default surcharge for cache-WRITE tokens when a model has no explicit
# cache-write rate: Anthropic's 5-minute ephemeral cache write is priced at
# roughly 1.25x normal input (the 1-hour variant is ~2x, but 5-minute is what
# this app uses — see providers._anthropic_system). A premium, not a discount:
# establishing a cache entry costs the provider extra work up front, which is
# the tradeoff for every later call within the TTL reading it back at a
# fraction of the price.
_DEFAULT_CACHE_WRITE_MULTIPLIER = 1.25

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

# An embed() call (semantic_cache/memory/the RAG library) bills per input
# token like a normal model call, but the OpenAI embeddings response's token
# count is discarded by the shared embed() helper (it only keeps the
# vector) — same "flat per-char estimate" workaround as speech/transcription
# above, sized for text-embedding-3-small's $0.02/1M-token rate at ~4
# chars/token (≈ $0.000005 per 1K chars). Override via
# EMBEDDING_COST_PER_1K_CHARS_USD for a different embedding model/exact figures.
_DEFAULT_EMBEDDING_COST_PER_1K_CHARS_USD = 0.000005


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


def _cache_write_multiplier() -> float:
    raw = (os.getenv("CACHE_WRITE_MULTIPLIER") or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_CACHE_WRITE_MULTIPLIER
    except ValueError:
        return _DEFAULT_CACHE_WRITE_MULTIPLIER
    if not math.isfinite(value):
        return _DEFAULT_CACHE_WRITE_MULTIPLIER
    # A premium, not a discount — floor at 1x (never cheaper than normal
    # input), but no upper clamp since providers' real write premiums vary.
    return max(value, 1.0)


def _pricing() -> dict[str, tuple[float, ...]]:
    """Layered precedence, lowest to highest: the self-updating catalog's
    cached feed (see app/model_catalog.py — {} when disabled or never
    synced), then this module's hand-curated defaults, then MODEL_PRICING —
    an explicit user override always wins over both auto-sourced layers.
    """
    table: dict[str, tuple[float, ...]] = {}
    try:
        from .model_catalog import cached_pricing

        table.update(cached_pricing())
    except Exception:
        pass
    table.update(_DEFAULT_PRICING)
    raw = (os.getenv("MODEL_PRICING") or "").strip()
    if raw:
        try:
            for model, values in json.loads(raw).items():
                rates = tuple(float(v) for v in values[:4])
                if len(rates) >= 2:
                    table[model] = rates
        except (ValueError, TypeError, KeyError, IndexError):
            pass
    return table


def estimate_cost(model: str, usage: Usage | None) -> float | None:
    """Estimated USD cost for a call, or None if the model isn't priced.

    `input_tokens` is treated as the TOTAL prompt size across all three tiers,
    with `cached_input_tokens`/`cache_write_input_tokens` breaking out subsets
    of it (see Usage's docstrings) — never additive on top of `input_tokens`.
    Cached (read) tokens are billed at the model's cached-input rate (a 3rd
    price value) if given, else input_rate * CACHED_INPUT_MULTIPLIER. Cache-
    write tokens are billed at the model's cache-write rate (a 4th price value)
    if given, else input_rate * CACHE_WRITE_MULTIPLIER. With both at 0 the
    result is identical to plain input+output pricing.
    """
    if usage is None:
        return None
    # A model the operator explicitly configured as free-tier (see
    # app/free_tier.py) is $0 for THIS purpose regardless of whatever normal
    # per-token price it has in the table below — that price describes what
    # the model costs OUTSIDE the free-tier quota (e.g. a Gemini model used
    # via a direct pin/smart-tier call), not what a free-tier-routed call to
    # it costs. Checked before the table lookup, not in the "unpriced model"
    # branch further down, since a free-tier model is very often ALSO a
    # normally-priced one elsewhere (unlike Ollama, which is never priced at
    # all). Deferred import: app.free_tier imports app.settings, which
    # imports app.providers, which imports Usage from this very module — a
    # module-level import here would be circular.
    from .free_tier import is_free_tier_model

    if is_free_tier_model(model):
        return 0.0
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
    cache_write_rate = (
        price[3] if len(price) > 3 else input_rate * _cache_write_multiplier()
    )

    # cached_input_tokens and cache_write_input_tokens are both subsets of
    # input_tokens (never additive on top of it) — guard against bad data by
    # clamping their combined total to input_tokens, cached first so a
    # pathological overlap favors the discount over the surcharge.
    cached = max(0, min(usage.cached_input_tokens, usage.input_tokens))
    cache_write = max(
        0, min(usage.cache_write_input_tokens, usage.input_tokens - cached)
    )
    uncached = usage.input_tokens - cached - cache_write

    cost = (
        uncached / 1_000_000 * input_rate
        + cached / 1_000_000 * cached_rate
        + cache_write / 1_000_000 * cache_write_rate
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


def estimate_embedding_cost(text: str) -> float:
    """Rough USD cost estimate for one embed() call, priced per 1K input
    characters — see _DEFAULT_EMBEDDING_COST_PER_1K_CHARS_USD for why this
    is a char-based estimate rather than an exact token count."""
    rate = _positive_float_env(
        "EMBEDDING_COST_PER_1K_CHARS_USD", _DEFAULT_EMBEDDING_COST_PER_1K_CHARS_USD
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
