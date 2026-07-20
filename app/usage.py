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
        # Fall back to a bare name (drop an optional provider prefix).
        bare = model.split("/", 1)[-1]
        price = table.get(bare)
    if price is None:
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
