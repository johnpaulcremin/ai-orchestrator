"""Free-first routing: try provider-hosted free-tier models (Gemini's free
API tier, Groq's free tier, OpenRouter's `:free` models, ...) before falling
through to the configured budget/fast tier — a free-tier model, while it
still has quota, costs literally nothing, the same $0 treatment this app
already gives a local Ollama model (see usage.estimate_cost).

Deliberately user-configured, not hardcoded: real free-tier request limits
vary by provider, change over time, and depend on the caller's own account
(a Gemini key that's ever been billed gets different limits than a brand-new
one) — baking in "today's" published numbers would be guessing at values
that don't apply to every deployment and would go stale regardless.
FREE_TIER_MODELS lists which models to try, in order; FREE_TIER_QUOTA_<NAME>
(or the FREE_TIER_DEFAULT_QUOTA fallback) says how many requests per UTC day
each is allowed before this app stops routing to it for the rest of the day.

No provider exposes a live "how much free quota do I have left" API for any
of these, so this is a simple daily request counter this app keeps itself
(see database.free_tier_usage), reset implicitly at UTC midnight — an
approximation of the account's real remaining allowance, not a live query
against the provider. Only requests actually dispatched via the free-tier
routing path (see orchestrator._apply_free_tier_override) increment it: a
conversation manually pinned to the same model name bypasses this app's own
tracking (still real usage against the provider's actual quota, just
untracked here) — a known, accepted rough edge for a v1.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from . import database
from .settings import bool_setting

_DEFAULT_QUOTA = 100
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9]+")


def configured_models() -> list[str]:
    """The ordered list of free-tier models to try, from FREE_TIER_MODELS
    (comma-separated). Empty when unset — the feature is then fully off
    regardless of the FREE_TIER_ROUTING flag, since there's nothing to route
    to."""
    raw = (os.getenv("FREE_TIER_MODELS") or "").strip()
    if not raw:
        return []
    return [m.strip() for m in raw.split(",") if m.strip()]


def enabled() -> bool:
    """Opt-in: at least one model configured AND the runtime feature flag
    (default on — this never costs money, only ever saves it, same
    reasoning as IMAGE_DOWNSCALE/DB_BACKUP defaulting on) not turned off."""
    return bool(configured_models()) and bool_setting("FREE_TIER_ROUTING", True)


def is_free_tier_model(model: str) -> bool:
    """Whether `model` is configured as a free-tier model — used by
    usage.estimate_cost to price it at $0 regardless of which path dispatched
    to it (a manual pin to the same model name is still genuinely free, even
    though it doesn't count against this app's own tracked quota above)."""
    return model in configured_models()


def _env_safe_name(model: str) -> str:
    return _SAFE_NAME_RE.sub("_", model).strip("_").upper()


def daily_quota(model: str) -> int:
    """This model's configured daily request quota: FREE_TIER_QUOTA_<NAME>
    if set, else FREE_TIER_DEFAULT_QUOTA, else the built-in default (100)."""
    per_model = (os.getenv(f"FREE_TIER_QUOTA_{_env_safe_name(model)}") or "").strip()
    raw = per_model or (os.getenv("FREE_TIER_DEFAULT_QUOTA") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_QUOTA
    except ValueError:
        return _DEFAULT_QUOTA
    return value if value > 0 else _DEFAULT_QUOTA


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def used_today(model: str) -> int:
    return database.free_tier_usage_count(model, _today())


def has_quota_remaining(model: str) -> bool:
    return used_today(model) < daily_quota(model)


def record_use(model: str) -> None:
    database.free_tier_usage_increment(model, _today())


def pick_available_model() -> str | None:
    """The first configured free-tier model (in order) that still has quota
    remaining today, or None if none are configured or all are exhausted."""
    for model in configured_models():
        if has_quota_remaining(model):
            return model
    return None
