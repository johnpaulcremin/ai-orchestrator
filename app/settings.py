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


def category_prompt_key(category: str) -> str:
    return f"CATEGORY_PROMPT_{category.upper()}"


# A free-text role/system prompt per task category (e.g. CATEGORY_PROMPT_CODING
# for a coder persona), same override > env > default resolution chain as
# MODEL_<CATEGORY> — see orchestrator.apply_category_role_prompt. Every default
# is "" (no role prompt), so an unconfigured deployment behaves exactly as
# before this feature existed.
PROMPT_KEYS: tuple[str, ...] = tuple(
    category_prompt_key(category) for category in sorted(ALL_CATEGORIES)
)

# Same cap as a per-conversation custom instructions field (schemas._MAX_SYSTEM_PROMPT_CHARS).
MAX_PROMPT_LEN = 4_000

# The free-lane model list and its default daily quota (see app/free_tier.py)
# — editable at runtime like a model tier, rather than .env-only, so trying a
# different ordering or provider needs no restart. FREE_TIER_QUOTA_<MODEL>
# per-model overrides stay env-only (an advanced/rare case not worth a
# per-model UI row).
FREE_LANE_KEYS: tuple[str, ...] = ("FREE_TIER_MODELS", "FREE_TIER_DEFAULT_QUOTA")
FREE_LANE_LABELS: dict[str, str] = {
    "FREE_TIER_MODELS": "Free-tier models (ordered, comma-separated)",
    "FREE_TIER_DEFAULT_QUOTA": "Default daily quota per model",
}
_MAX_FREE_TIER_MODELS_LEN = 2_000

# Data retention (see app/retention.py) — how long the spend_log/
# avoided_cost_log/feedback_log ledgers keep row-per-call detail before it's
# rolled into a monthly aggregate and pruned, and how long a share link
# lives by default. Both stored as plain integer strings via the same
# override > env > default chain as a model tier, resolved to int only at
# the point of use (same "string in the settings table, parsed by the
# caller" convention as FREE_TIER_DEFAULT_QUOTA).
RETENTION_KEYS: tuple[str, ...] = ("RETENTION_DAYS_DETAIL", "SHARE_EXPIRY_DAYS")
RETENTION_LABELS: dict[str, str] = {
    "RETENTION_DAYS_DETAIL": "Ledger detail retention (days, 0 = forever)",
    "SHARE_EXPIRY_DAYS": "Default share-link expiry (days, blank = never)",
}
# RETENTION_DAYS_DETAIL's default ("365") is a real value (not ""), matching
# TIER_DEFAULTS' convention of describe_settings always reporting SOME
# effective default; SHARE_EXPIRY_DAYS defaults to "" (no expiry) since an
# unset share link living until revoked is this app's existing behavior,
# unchanged unless an operator opts in.
RETENTION_DEFAULTS: dict[str, str] = {
    "RETENTION_DAYS_DETAIL": "365",
    "SHARE_EXPIRY_DAYS": "",
}

# The optional, cost-affecting tool flags — each normally requires editing
# .env and restarting to change; making them live-editable here means turning
# one off (e.g. to stop CODE_EXECUTION spend mid-session) needs no restart,
# same as re-pointing a model tier.
FEATURE_FLAG_KEYS: tuple[str, ...] = (
    "WEB_SEARCH",
    "IMAGE_GENERATION",
    "CODE_EXECUTION",
    "MODERATION",
    "CROSS_CONVERSATION_MEMORY",
    "FACT_CHECK",
    "MATH_SOLVE",
    "IMAGE_DOWNSCALE",
    "OCR_REPLACEMENT",
    "CONCISE_MODE",
    "SEMANTIC_CACHE",
    "MODEL_CATALOG_SYNC",
    "DB_BACKUP",
    "FREE_TIER_ROUTING",
    "RAG_LIBRARY",
    "FREE_LANE_SMART",
    "ACADEMIC_SEARCH",
    "SELF_DESCRIBE",
)

FEATURE_FLAG_LABELS: dict[str, str] = {
    "WEB_SEARCH": "Web search retrieval",
    "IMAGE_GENERATION": "Image generation",
    "CODE_EXECUTION": "Code execution",
    "MODERATION": "Moderation safety net",
    "CROSS_CONVERSATION_MEMORY": "Cross-conversation memory",
    "FACT_CHECK": "Fact-check lookup",
    "MATH_SOLVE": "Precision math (SymPy)",
    "IMAGE_DOWNSCALE": "Automatic image downscaling",
    "OCR_REPLACEMENT": "Automatic OCR replacement",
    "CONCISE_MODE": "Concise answers",
    "SEMANTIC_CACHE": "Semantic (paraphrase) response cache",
    "MODEL_CATALOG_SYNC": "Self-updating model pricing catalog",
    "DB_BACKUP": "Rotating periodic database backups",
    "FREE_TIER_ROUTING": "Free-tier model routing",
    "RAG_LIBRARY": "Document library (RAG)",
    "FREE_LANE_SMART": "Free-tier routing for smart-tier requests",
    "ACADEMIC_SEARCH": "Academic/scholarly search lookup",
    "SELF_DESCRIBE": "Self-description (capabilities grounding)",
}

FEATURE_FLAG_DESCRIPTIONS: dict[str, str] = {
    "WEB_SEARCH": "Grounds freshness-sensitive auto-mode answers in live web results.",
    "IMAGE_GENERATION": "Lets the model generate images when asked.",
    "CODE_EXECUTION": "Lets the model run Python to verify a calculation or snippet.",
    "MODERATION": "Checks each question with OpenAI's moderation endpoint before any model call — an independent check on what the user sent, not on what a model decides to say. A flagged question is refused before any budget is spent.",
    "CROSS_CONVERSATION_MEMORY": "Recalls relevant exchanges from your other conversations (via embedding similarity) and folds them into a new turn as extra context — the model uses its own judgment on whether they're actually relevant.",
    "FACT_CHECK": "Looks up published fact-checks (Snopes, PolitiFact, ...) for a claim-verification question via Google's Fact Check Tools API, independent of which model answers. Requires GOOGLE_FACT_CHECK_API_KEY.",
    "MATH_SOLVE": "Offers the model a tool to get an exact, verified algebra/calculus result from SymPy instead of computing one itself. Free, local, zero LLM tokens — no external API or key needed.",
    "IMAGE_DOWNSCALE": "Resizes large attached images down before sending, unless the question implies fine detail matters.",
    "OCR_REPLACEMENT": "Sends confidently-extracted text instead of an attached image when it's mostly text (requires Tesseract installed locally; silently no-ops otherwise).",
    "CONCISE_MODE": "Instructs the model to answer tersely — no preamble, filler, or hedging. Output tokens usually cost far more than input tokens.",
    "SEMANTIC_CACHE": "Serves a cached answer for a paraphrased repeat of a context-free question (no conversation history/instructions behind it), via embedding similarity. High-confidence threshold by default; a wrong match is worse than a miss, so this stays opt-in.",
    "MODEL_CATALOG_SYNC": "Pulls LiteLLM's published pricing feed to keep model prices current instead of relying only on the hand-maintained defaults. Along with FACT_CHECK, the only things in this app that call a server other than a configured LLM provider, so both are opt-in.",
    "DB_BACKUP": "Periodically copies the whole database file (checked whenever the sidebar loads, actually runs at most once per DB_BACKUP_INTERVAL_HOURS) and keeps the last DB_BACKUP_MAX_COUNT of them, deleting older ones. A local file copy, never a network call.",
    "FREE_TIER_ROUTING": "Routes fast/budget-tier traffic to a configured provider free-tier model (FREE_TIER_MODELS) before the paid tier, while a self-tracked daily quota lasts. Never touches smart-tier requests or an explicitly forced model.",
    "RAG_LIBRARY": "Recalls relevant chunks from your uploaded reference documents (via embedding similarity) and folds them into a new turn as extra context, alongside cross-conversation memory — the model uses its own judgment on whether they're actually relevant. Never engages when your library is empty.",
    "FREE_LANE_SMART": "Lets free-tier routing (FREE_TIER_ROUTING) also substitute for smart-tier requests, not just fast/budget. Off by default — a smart-tier request is one where quality was chosen deliberately, so silently downgrading it to a free-tier model needs an explicit opt-in.",
    "ACADEMIC_SEARCH": "Looks up scholarly literature (via OpenAlex, free and keyless) for a research-literature question, independent of which model answers — same standalone-call pattern as FACT_CHECK.",
    "SELF_DESCRIBE": "Offers an app_capabilities tool the model can call for a 'what can you do' / 'what models do you use' style question (OpenAI/Anthropic), or a phrase-heuristic fallback note otherwise — grounds the answer in this app's real configuration (models, enabled features, limits, your remaining budget) instead of the model guessing about a private app it has no training data on.",
}

# WEB_SEARCH/IMAGE_GENERATION/CODE_EXECUTION default to off — each spends
# tokens/money the operator must opt into. IMAGE_DOWNSCALE/OCR_REPLACEMENT are
# the opposite: they only ever REDUCE what a vision call costs (and OCR only
# ever engages if Tesseract is actually installed), gated by their own
# fine-detail/confidence heuristics — so they default ON, opt-out rather than
# opt-in, per the design that prompted them (automatic, no user decision
# required unless they want to turn one off). DB_BACKUP/FREE_TIER_ROUTING
# default ON for the same reason: a local file copy, and routing to a model
# the operator explicitly listed as free, neither ever touch answering
# behavior or cost in a way the operator didn't already opt into by
# configuring DB_BACKUP_* / FREE_TIER_MODELS in the first place — the flag
# just lets it be paused without unsetting that config. CONCISE_MODE,
# SEMANTIC_CACHE, and MODEL_CATALOG_SYNC default off like the first group:
# CONCISE_MODE changes what the model actually SAYS, not just what a call
# costs; SEMANTIC_CACHE can serve a wrong answer for a merely-similar-sounding
# question if it ever mismatches; MODEL_CATALOG_SYNC is the only thing here
# that calls a server other than a configured LLM provider — all three need
# an explicit opt-in rather than defaulting on.
FEATURE_FLAG_DEFAULTS: dict[str, bool] = {
    key: key in ("IMAGE_DOWNSCALE", "OCR_REPLACEMENT", "DB_BACKUP", "FREE_TIER_ROUTING")
    for key in FEATURE_FLAG_KEYS
}

SETTABLE_KEYS: frozenset[str] = (
    frozenset(TIER_KEYS)
    | frozenset(CATEGORY_KEYS)
    | frozenset(FEATURE_FLAG_KEYS)
    | frozenset(PROMPT_KEYS)
    | frozenset(FREE_LANE_KEYS)
    | frozenset(RETENTION_KEYS)
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


def validate_prompt_value(value: str) -> str:
    """Clean and validate a per-category role-prompt value. Raises ValueError
    if malformed.

    The empty string is valid and means "clear this override" (same contract
    as validate_model_value/validate_bool_value). Otherwise free text — no
    character restriction, unlike a model name — capped at the same length a
    per-conversation custom-instructions field allows
    (schemas._MAX_SYSTEM_PROMPT_CHARS), since it folds into the same kind of
    system-prompt context.
    """
    cleaned = value.strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_PROMPT_LEN:
        raise ValueError(f"role prompt too long (max {MAX_PROMPT_LEN} characters)")
    return cleaned


def validate_free_tier_models_value(value: str) -> str:
    """Clean and validate a FREE_TIER_MODELS value: a comma-separated,
    ordered list of model names, each held to the same character rules as a
    single model name (validate_model_value). Raises ValueError if
    malformed. The empty string is valid and means "clear this override"
    (same contract as validate_model_value)."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_FREE_TIER_MODELS_LEN:
        raise ValueError(
            f"free-tier model list too long (max {_MAX_FREE_TIER_MODELS_LEN} characters)"
        )
    models = [m.strip() for m in cleaned.split(",") if m.strip()]
    if not models:
        return ""
    for model in models:
        if not _MODEL_NAME_RE.match(model):
            raise ValueError(
                f"invalid model name {model!r} — may contain only letters, "
                "digits, and . _ - : / characters"
            )
    return ",".join(models)


def validate_free_tier_quota_value(value: str) -> str:
    """Clean and validate a FREE_TIER_DEFAULT_QUOTA value: a positive
    integer. Raises ValueError if malformed. The empty string is valid and
    means "clear this override"."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        parsed = int(cleaned)
    except ValueError as err:
        raise ValueError("quota must be a whole number") from err
    if parsed <= 0:
        raise ValueError("quota must be a positive number")
    return str(parsed)


def validate_retention_days_detail_value(value: str) -> str:
    """Clean and validate a RETENTION_DAYS_DETAIL value: a non-negative
    integer, 0 meaning "keep detail forever" (never prune). The empty
    string is valid and means "clear this override" (falls back to the
    365-day default), same contract as every other validator here."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        parsed = int(cleaned)
    except ValueError as err:
        raise ValueError("retention days must be a whole number") from err
    if parsed < 0:
        raise ValueError("retention days must be 0 or a positive number")
    return str(parsed)


def validate_share_expiry_days_value(value: str) -> str:
    """Clean and validate a SHARE_EXPIRY_DAYS value: a positive integer, or
    empty meaning "no default expiry" (a share link lives until revoked,
    this app's existing behavior)."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        parsed = int(cleaned)
    except ValueError as err:
        raise ValueError("expiry days must be a whole number") from err
    if parsed <= 0:
        raise ValueError("expiry days must be a positive number")
    return str(parsed)


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

    prompts: list[dict[str, Any]] = []
    for category in sorted(ALL_CATEGORIES):
        key = category_prompt_key(category)
        prompts.append(
            {
                "key": key,
                "category": category,
                "label": CATEGORY_LABELS.get(category, category),
                "effective_prompt": model_setting(key, "", overrides),
                "source": _source(key, overrides),
                "override": overrides.get(key),
                "env": (os.getenv(key) or "").strip() or None,
                "default": "",
            }
        )

    free_lane: list[dict[str, Any]] = []
    free_lane_defaults = {"FREE_TIER_MODELS": "", "FREE_TIER_DEFAULT_QUOTA": "100"}
    for key in FREE_LANE_KEYS:
        default = free_lane_defaults[key]
        free_lane.append(
            {
                "key": key,
                "label": FREE_LANE_LABELS[key],
                "effective_value": model_setting(key, default, overrides),
                "source": _source(key, overrides),
                "override": overrides.get(key),
                "env": (os.getenv(key) or "").strip() or None,
                "default": default,
            }
        )

    retention: list[dict[str, Any]] = []
    for key in RETENTION_KEYS:
        default = RETENTION_DEFAULTS[key]
        retention.append(
            {
                "key": key,
                "label": RETENTION_LABELS[key],
                "effective_value": model_setting(key, default, overrides),
                "source": _source(key, overrides),
                "override": overrides.get(key),
                "env": (os.getenv(key) or "").strip() or None,
                "default": default,
            }
        )

    return {
        "editable": settings_writable(),
        "tiers": tiers,
        "categories": categories,
        "features": features,
        "prompts": prompts,
        "free_lane": free_lane,
        "retention": retention,
    }
