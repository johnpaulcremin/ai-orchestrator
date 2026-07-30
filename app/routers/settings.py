"""Runtime-editable settings, plus the operational cache/catalog admin
endpoints that live alongside them in the Settings UI panel: response
cache, semantic cache, and the self-updating model-pricing catalog.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from .. import cache, free_tier, memory, model_catalog, semantic_cache
from ..auth import current_owner
from ..database import clear_settings, delete_setting, set_setting
from ..schemas import ModelCatalogStatus, SettingUpdate
from ..self_describe import capabilities_snapshot
from ..security import admin_usernames, jwt_enabled, registration_allowed
from ..settings import (
    FEATURE_FLAG_KEYS,
    PROMPT_KEYS,
    SETTABLE_KEYS,
    describe_settings,
    settings_writable,
    validate_bool_value,
    validate_free_tier_models_value,
    validate_free_tier_quota_value,
    validate_model_value,
    validate_prompt_value,
)
from .deps import router


def _require_writable_settings() -> None:
    if not settings_writable():
        raise HTTPException(
            status_code=403,
            detail="Settings editing is disabled (ALLOW_SETTINGS_WRITE=false).",
        )


def _require_admin(owner: str | None) -> None:
    """Block settings mutation from an untrusted self-registered account.

    This app has no other admin/role concept — every authenticated caller is
    otherwise equally privileged. That's fine when the user set is
    operator-provisioned (registration closed) or auth is a single shared
    static token, but with JWT auth enabled AND open registration, anyone can
    self-register their own credential (see registration_allowed()) and would
    otherwise inherit the same settings-write rights as the operator. Gate
    only that one path; every other configuration keeps today's behavior.
    """
    if not jwt_enabled() or not registration_allowed():
        return
    admins = admin_usernames()
    if not admins or owner is None or owner.strip().lower() not in admins:
        raise HTTPException(
            status_code=403,
            detail=(
                "Settings editing requires an admin account while open "
                "registration is enabled. Set ADMIN_USERNAMES, or set "
                "ALLOW_REGISTRATION=false."
            ),
        )


def _require_settable_key(key: str) -> None:
    if key not in SETTABLE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"'{key}' is not an editable setting.",
        )


@router.get("/v1/settings")
def get_settings_view():
    """The full resolved model map (tiers + task categories) for the UI."""
    return describe_settings()


@router.put("/v1/settings/{key}")
def put_setting(
    key: str, req: SettingUpdate, owner: str | None = Depends(current_owner)
):
    """Set a model or feature-flag override for a key, or clear it when the
    value is empty."""
    _require_writable_settings()
    _require_admin(owner)
    _require_settable_key(key)

    validator = (
        validate_bool_value
        if key in FEATURE_FLAG_KEYS
        else validate_prompt_value
        if key in PROMPT_KEYS
        else validate_free_tier_models_value
        if key == "FREE_TIER_MODELS"
        else validate_free_tier_quota_value
        if key == "FREE_TIER_DEFAULT_QUOTA"
        else validate_model_value
    )
    try:
        value = validator(req.value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if value:
        set_setting(key, value)
    else:
        delete_setting(key)

    return describe_settings()


@router.delete("/v1/settings/{key}")
def clear_setting(key: str, owner: str | None = Depends(current_owner)):
    """Clear a single override, reverting the key to its env var / default."""
    _require_writable_settings()
    _require_admin(owner)
    _require_settable_key(key)
    delete_setting(key)
    return describe_settings()


@router.post("/v1/settings/reset")
def reset_settings(owner: str | None = Depends(current_owner)):
    """Clear every override, reverting the whole map to env vars / defaults."""
    _require_writable_settings()
    _require_admin(owner)
    clear_settings()
    return describe_settings()


@router.get("/v1/cache")
def cache_info():
    """Response-cache status: enabled, entry count, TTL, and size cap."""
    return cache.stats()


@router.delete("/v1/cache")
def clear_cache():
    """Empty the response cache so subsequent prompts hit the model again."""
    return {"cleared": cache.clear(), **cache.stats()}


@router.get("/v1/semantic-cache")
def semantic_cache_info():
    """Semantic (paraphrase) cache status: enabled, entry count, similarity
    threshold, and size cap. See app/semantic_cache.py."""
    return semantic_cache.stats()


@router.delete("/v1/semantic-cache")
def clear_semantic_cache():
    """Empty the semantic cache so subsequent paraphrased prompts hit the
    model (or the exact cache) again."""
    return {"cleared": semantic_cache.clear(), **semantic_cache.stats()}


@router.get("/v1/memory")
def memory_info():
    """Cross-conversation memory status: enabled, entry count, similarity
    threshold, top-k, and per-owner size cap. See app/memory.py."""
    return memory.stats()


@router.delete("/v1/memory")
def clear_memory():
    """Empty cross-conversation memory (every owner) so no past exchange is
    recalled into a future turn until new ones accumulate again."""
    return {"cleared": memory.clear(), **memory.stats()}


@router.get("/v1/free-tier")
def free_tier_status():
    """Per-configured-model free-lane quota status (see app/free_tier.py) —
    what the Usage panel's "free lane remaining today" section shows."""
    return {"enabled": free_tier.enabled(), "models": free_tier.status()}


@router.get("/v1/capabilities")
def capabilities(owner: str | None = Depends(current_owner)):
    """This app's real self-description: version, model map, feature flags,
    known request limits, this caller's own remaining per-owner budget, and
    free-lane quota status — same data SELF_DESCRIBE folds into an answer
    for a "what can you do" style question (see app/self_describe.py)."""
    return capabilities_snapshot(owner)


@router.get("/v1/model-catalog", response_model=ModelCatalogStatus)
def model_catalog_status():
    """Self-updating model/pricing catalog status (see app/model_catalog.py).
    A DB-only read UNLESS the catalog is enabled and stale, in which case
    this triggers exactly one sync — opening the Settings panel is what "on
    a schedule" means here, since this app has no background scheduler."""
    return model_catalog.sync_if_stale()


@router.post("/v1/model-catalog/sync", response_model=ModelCatalogStatus)
def model_catalog_sync():
    """Force a sync now, ignoring staleness — the "Sync now" button. A
    no-op returning the current status when MODEL_CATALOG_SYNC is off."""
    return model_catalog.sync_now()
