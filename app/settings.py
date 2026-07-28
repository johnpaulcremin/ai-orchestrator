from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from . import database
from .categories import ALL_CATEGORIES, CATEGORY_LABELS, tier_of
from .providers import key_env_for, provider_of

# --- Which keys the settings UI is allowed to edit ---------------------------
# Only model-selection keys are settable at runtime. Credential keys
# (OPENAI_API_KEY, ANTHROPIC_API_KEY, ...) are deliberately NOT in this set, so
# the settings API can never be used to write or overwrite a secret.

TIER_KEYS: tuple[str, ...] = (
    "OPENAI_MODEL",
    "OPENAI_MODEL_ROUTER",
    "OPENAI_MODEL_BUDGET",
    "OPENAI_MODEL_FAST",
    "OPENAI_MODEL_SMART",
    "OPENAI_MODEL_FALLBACK",
)

TIER_LABELS: dict[str, str] = {
    "OPENAI_MODEL": "Base / default",
    "OPENAI_MODEL_ROUTER": "Router (auto classifier)",
    "OPENAI_MODEL_BUDGET": "Budget tier",
    "OPENAI_MODEL_FAST": "Fast tier",
    "OPENAI_MODEL_SMART": "Smart tier",
    "OPENAI_MODEL_FALLBACK": "Fallback",
}

# Code defaults, mirroring routing.py, used only for display of the "default"
# source. Empty string means "inherits the base/tier model" (budget: unset =>
# the tier is off and low-complexity tasks stay on fast).
TIER_DEFAULTS: dict[str, str] = {
    "OPENAI_MODEL": "gpt-5",
    "OPENAI_MODEL_ROUTER": "gpt-5-nano",
    "OPENAI_MODEL_BUDGET": "",
    "OPENAI_MODEL_FAST": "",
    "OPENAI_MODEL_SMART": "",
    "OPENAI_MODEL_FALLBACK": "",
}


def category_key(category: str) -> str:
    return f"MODEL_{category.upper()}"


CATEGORY_KEYS: tuple[str, ...] = tuple(
    category_key(category) for category in sorted(ALL_CATEGORIES)
)

# The optional, cost-affecting tool flags — each normally requires editing
# .env and restarting to change; making them live-editable here means turning
# one off (e.g. to stop CODE_EXECUTION spend mid-session) needs no restart,
# same as re-pointing a model tier.
FEATURE_FLAG_KEYS: tuple[str, ...] = (
    "WEB_SEARCH",
    "IMAGE_GENERATION",
    "CODE_EXECUTION",
    "IMAGE_DOWNSCALE",
    "OCR_REPLACEMENT",
    "CONCISE_MODE",
)

FEATURE_FLAG_LABELS: dict[str, str] = {
    "WEB_SEARCH": "Web search retrieval",
    "IMAGE_GENERATION": "Image generation",
    "CODE_EXECUTION": "Code execution",
    "IMAGE_DOWNSCALE": "Automatic image downscaling",
    "OCR_REPLACEMENT": "Automatic OCR replacement",
    "CONCISE_MODE": "Concise answers",
}

FEATURE_FLAG_DESCRIPTIONS: dict[str, str] = {
    "WEB_SEARCH": "Grounds freshness-sensitive auto-mode answers in live web results.",
    "IMAGE_GENERATION": "Lets the model generate images when asked.",
    "CODE_EXECUTION": "Lets the model run Python to verify a calculation or snippet.",
    "IMAGE_DOWNSCALE": "Resizes large attached images down before sending, unless the question implies fine detail matters.",
    "OCR_REPLACEMENT": "Sends confidently-extracted text instead of an attached image when it's mostly text (requires Tesseract installed locally; silently no-ops otherwise).",
    "CONCISE_MODE": "Instructs the model to answer tersely — no preamble, filler, or hedging. Output tokens usually cost far more than input tokens.",
}

# WEB_SEARCH/IMAGE_GENERATION/CODE_EXECUTION default to off — each spends
# tokens/money the operator must opt into. IMAGE_DOWNSCALE/OCR_REPLACEMENT are
# the opposite: they only ever REDUCE what a vision call costs (and OCR only
# ever engages if Tesseract is actually installed), gated by their own
# fine-detail/confidence heuristics — so they default ON, opt-out rather than
# opt-in, per the design that prompted them (automatic, no user decision
# required unless they want to turn one off). CONCISE_MODE defaults off like
# the first group: it changes what the model actually SAYS, not just what a
# call costs, so — unlike the two above — it needs an explicit opt-in.
FEATURE_FLAG_DEFAULTS: dict[str, bool] = {
    key: key in ("IMAGE_DOWNSCALE", "OCR_REPLACEMENT") for key in FEATURE_FLAG_KEYS
}

SETTABLE_KEYS: frozenset[str] = (
    frozenset(TIER_KEYS) | frozenset(CATEGORY_KEYS) | frozenset(FEATURE_FLAG_KEYS)
)

# A model name: letters, digits, and the separators real model ids use
# (e.g. "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"). No spaces.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:\-/]+$")
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_MODEL_LEN = 200


# --- Resolution: DB override > env var > code default ------------------------


def get_model_overrides() -> dict[str, str]:
    """The persisted, non-empty model overrides for settable keys.

    Returns {} if the settings table does not exist yet (fresh DB) so routing
    behaves exactly as env-only until a value is saved.
    """
    try:
        raw = database.get_settings()
    except sqlite3.Error:
        # No settings table yet (fresh DB) or the DB is unavailable: behave as
        # env-only until a value is saved, rather than breaking routing.
        return {}
    return {
        key: value.strip()
        for key, value in raw.items()
        if key in SETTABLE_KEYS and value and value.strip()
    }


def model_setting(
    key: str, default: str = "", overrides: dict[str, str] | None = None
) -> str:
    """Resolve a model key: DB override, then env var, then the code default."""
    if overrides is None:
        overrides = get_model_overrides()

    override = overrides.get(key)
    if override and override.strip():
        return override.strip()

    env_value = os.getenv(key)
    if env_value and env_value.strip():
        return env_value.strip()

    return default


_FALSY = {"false", "0", "no", "off"}


def bool_setting(
    key: str, default: bool = False, overrides: dict[str, str] | None = None
) -> bool:
    """Resolve a feature-flag key the same override > env > default chain as
    model_setting, but as a bool instead of a string."""
    if overrides is None:
        overrides = get_model_overrides()

    override = overrides.get(key)
    if override and override.strip():
        return override.strip().lower() not in _FALSY

    env_value = os.getenv(key)
    if env_value and env_value.strip():
        return env_value.strip().lower() not in _FALSY

    return default


def settings_writable() -> bool:
    """Whether the settings API may mutate the map (ALLOW_SETTINGS_WRITE)."""
    raw = (os.getenv("ALLOW_SETTINGS_WRITE") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def validate_model_value(value: str) -> str:
    """Clean and validate a model-name value. Raises ValueError if malformed.

    The empty string is valid and means "clear this override"; the caller
    decides whether an empty value clears or is rejected.
    """
    cleaned = value.strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_MODEL_LEN:
        raise ValueError(f"model name too long (max {MAX_MODEL_LEN} characters)")
    if not _MODEL_NAME_RE.match(cleaned):
        raise ValueError(
            "model name may contain only letters, digits, and . _ - : / characters"
        )
    return cleaned


def validate_bool_value(value: str) -> str:
    """Clean and validate a feature-flag value. Raises ValueError if malformed.

    The empty string is valid and means "clear this override" (same
    contract as validate_model_value); otherwise only true/false spellings
    are accepted, normalized to a canonical "true"/"false" for storage.
    """
    cleaned = value.strip().lower()
    if not cleaned:
        return ""
    if cleaned in _FALSY:
        return "false"
    if cleaned in {"true", "1", "yes", "on"}:
        return "true"
    raise ValueError('value must be "true" or "false"')


# --- Structured view for the settings UI -------------------------------------


def _key_present(key_env: str) -> bool | None:
    """True/False if we can name the credential env var; None if we can't
    (e.g. Bedrock's "AWS credentials")."""
    if not _ENV_VAR_RE.match(key_env):
        return None
    return bool((os.getenv(key_env) or "").strip())


def _credential_info(effective_model: str) -> dict[str, Any]:
    if not effective_model:
        return {"provider": "", "key_env": "", "key_present": None}
    key_env = key_env_for(effective_model)
    return {
        "provider": provider_of(effective_model),
        "key_env": key_env,
        "key_present": _key_present(key_env),
    }


def _source(key: str, overrides: dict[str, str]) -> str:
    if key in overrides:
        return "override"
    if (os.getenv(key) or "").strip():
        return "env"
    return "default"


def describe_settings() -> dict[str, Any]:
    """The full, resolved model map for the settings UI.

    Reports, for every tier and task category, the effective model and where it
    came from (a saved override, an env var, or the built-in default), plus the
    credential each effective model needs and whether that credential is set.
    """
    overrides = get_model_overrides()

    base = model_setting("OPENAI_MODEL", "gpt-5", overrides)
    fast = model_setting("OPENAI_MODEL_FAST", base, overrides)
    smart = model_setting("OPENAI_MODEL_SMART", base, overrides)

    tiers: list[dict[str, Any]] = []
    for key in TIER_KEYS:
        default = TIER_DEFAULTS[key]
        if key == "OPENAI_MODEL_FAST":
            effective = fast
        elif key == "OPENAI_MODEL_SMART":
            effective = smart
        else:
            effective = model_setting(key, default, overrides)
        tiers.append(
            {
                "key": key,
                "label": TIER_LABELS[key],
                "effective_model": effective,
                "source": _source(key, overrides),
                "override": overrides.get(key),
                "env": (os.getenv(key) or "").strip() or None,
                "default": default,
                **_credential_info(effective),
            }
        )

    categories: list[dict[str, Any]] = []
    for category in sorted(ALL_CATEGORIES):
        key = category_key(category)
        tier = tier_of(category)
        tier_model = smart if tier == "smart" else fast
        override_value = overrides.get(key)
        effective = model_setting(key, "", overrides) or tier_model
        categories.append(
            {
                "key": key,
                "category": category,
                "label": CATEGORY_LABELS.get(category, category),
                "tier": tier,
                "effective_model": effective,
                "source": _source(key, overrides),
                "override": override_value,
                "env": (os.getenv(key) or "").strip() or None,
                "inherits": tier_model,
                **_credential_info(effective),
            }
        )

    features: list[dict[str, Any]] = []
    for key in FEATURE_FLAG_KEYS:
        flag_default = FEATURE_FLAG_DEFAULTS[key]
        features.append(
            {
                "key": key,
                "label": FEATURE_FLAG_LABELS[key],
                "description": FEATURE_FLAG_DESCRIPTIONS[key],
                "effective_enabled": bool_setting(key, flag_default, overrides),
                "source": _source(key, overrides),
                "override": overrides.get(key),
                "env": (os.getenv(key) or "").strip() or None,
                "default": flag_default,
            }
        )

    return {
        "editable": settings_writable(),
        "tiers": tiers,
        "categories": categories,
        "features": features,
    }
