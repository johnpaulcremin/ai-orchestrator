from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class Usage:
    """Token counts for a single model call."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# Approximate list price in USD per 1,000,000 tokens, as (input, output).
# These change often — treat them as estimates and override via MODEL_PRICING
# (a JSON object of {"model": [input_per_mtok, output_per_mtok]}) if you need
# exact figures. Models not listed report tokens but no cost.
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-8": (15.0, 75.0),
    "claude-haiku-4-5": (0.80, 4.0),
    "gemini/gemini-flash-latest": (0.30, 2.50),
    "gemini/gemini-flash-lite-latest": (0.10, 0.40),
    "gemini/gemini-2.0-flash": (0.10, 0.40),
    "groq/llama-3.3-70b-versatile": (0.59, 0.79),
}


def _pricing() -> dict[str, tuple[float, float]]:
    table = dict(_DEFAULT_PRICING)
    raw = (os.getenv("MODEL_PRICING") or "").strip()
    if raw:
        try:
            for model, pair in json.loads(raw).items():
                table[model] = (float(pair[0]), float(pair[1]))
        except (ValueError, TypeError, KeyError, IndexError):
            pass
    return table


def estimate_cost(model: str, usage: Usage | None) -> float | None:
    """Estimated USD cost for a call, or None if the model isn't priced."""
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
    return (
        usage.input_tokens / 1_000_000 * price[0]
        + usage.output_tokens / 1_000_000 * price[1]
    )
