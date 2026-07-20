from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any

from . import database
from .categories import ALL_CATEGORIES
from .settings import category_key, get_model_overrides, model_setting

# Generation params (not model selection) that still change the answer. These
# are env-only; the tier/category models are resolved separately below.
_PARAM_ENV_KEYS = (
    "FAST_MAX_OUTPUT_TOKENS",
    "SMART_MAX_OUTPUT_TOKENS",
    "BUDGET_MAX_OUTPUT_TOKENS",
    "FAST_REASONING_EFFORT",
    "SMART_REASONING_EFFORT",
    "BUDGET_REASONING_EFFORT",
)


def enabled() -> bool:
    """Whether the response cache is active (RESPONSE_CACHE, default on)."""
    raw = (os.getenv("RESPONSE_CACHE") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def ttl_seconds() -> int:
    """Cache entry lifetime; 0 (the default) means entries never expire."""
    value = _int_env("RESPONSE_CACHE_TTL_SECONDS", 0)
    return value if value > 0 else 0


def max_entries() -> int:
    """Cap on stored entries (oldest evicted); 0 means unbounded."""
    value = _int_env("RESPONSE_CACHE_MAX_ENTRIES", 1000)
    return value if value > 0 else 0


def _config_signature() -> str:
    """A stable string over the fully-resolved model map + generation params.

    Every tier AND every task category is resolved through model_setting
    (override > env > default), so a change made through EITHER a saved override
    OR an env var — for any tier or category — yields a different signature and
    invalidates stale entries. Signing the resolved map (rather than a fixed list
    of env-var names) also captures any future routing input automatically.
    """
    overrides = get_model_overrides()
    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    tiers = {
        "OPENAI_MODEL": base,
        "OPENAI_MODEL_ROUTER": model_setting(
            "OPENAI_MODEL_ROUTER", "gpt-5-nano", overrides
        ),
        "OPENAI_MODEL_BUDGET": model_setting("OPENAI_MODEL_BUDGET", "", overrides),
        "OPENAI_MODEL_FAST": model_setting("OPENAI_MODEL_FAST", base, overrides),
        "OPENAI_MODEL_SMART": model_setting("OPENAI_MODEL_SMART", base, overrides),
        "OPENAI_MODEL_FALLBACK": model_setting("OPENAI_MODEL_FALLBACK", "", overrides),
    }
    categories = {
        cat: model_setting(category_key(cat), "", overrides)
        for cat in sorted(ALL_CATEGORIES)
    }
    params = {name: (os.getenv(name) or "") for name in _PARAM_ENV_KEYS}
    payload = {"tiers": tiers, "categories": categories, "params": params}
    return json.dumps(payload, sort_keys=True)


def make_key(question: str, mode: str) -> str:
    """Cache key: the prompt, the routing mode, and the model-config signature."""
    raw = "\x1f".join([mode, _config_signature(), question])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str) -> dict[str, Any] | None:
    """A fresh (non-expired) cache hit, or None. Records the hit on success."""
    if not enabled():
        return None
    try:
        row = database.cache_get(key)
    except sqlite3.Error:
        # A read failure must never break answering; fall through to the model.
        return None
    if row is None:
        return None
    ttl = ttl_seconds()
    if ttl and int(row.get("age_seconds") or 0) > ttl:
        try:
            database.cache_delete(key)
        except sqlite3.Error:
            pass
        return None
    try:
        database.cache_touch(key)
    except sqlite3.Error:
        # Best-effort telemetry only: a failed touch must not discard a valid
        # hit (which would waste a model call it existed to skip).
        pass
    return row


def put(
    key: str,
    question: str,
    mode: str,
    answer: str,
    mode_used: str | None,
    notes: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    """Store a successful answer, then evict beyond the size cap."""
    if not enabled() or not (answer or "").strip():
        return
    try:
        database.cache_put(
            key,
            question,
            mode,
            answer,
            mode_used,
            notes,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
        )
        cap = max_entries()
        if cap:
            count = database.cache_count()
            if count > cap:
                database.cache_delete_oldest(count - cap)
    except sqlite3.Error:
        # Best-effort: a failed cache write must not fail the request.
        return


def clear() -> int:
    try:
        return database.cache_clear()
    except sqlite3.Error:
        return 0


def stats() -> dict[str, Any]:
    try:
        entries = database.cache_count()
    except sqlite3.Error:
        entries = 0
    return {
        "enabled": enabled(),
        "entries": entries,
        "ttl_seconds": ttl_seconds(),
        "max_entries": max_entries(),
    }
