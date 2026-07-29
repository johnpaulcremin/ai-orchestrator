"""Unauthenticated service-identity endpoints: /, /health, /v1/status."""

from __future__ import annotations

import os

from ..budget import budget_status
from ..security import jwt_enabled, registration_allowed
from ..settings import model_setting
from .deps import public_router


@public_router.get("/")
def root():
    return {"status": "ok", "service": "ai-orchestrator"}


@public_router.get("/health")
def health():
    return {"status": "ok"}


@public_router.get("/v1/status")
def status():
    static_auth = bool(os.getenv("API_AUTH_TOKEN", "").strip())
    base_model = model_setting("OPENAI_MODEL", "gpt-5")
    return {
        "status": "ok",
        "service": "ai-orchestrator",
        "version": "0.1.0",
        "auth_enabled": static_auth or jwt_enabled(),
        "jwt_enabled": jwt_enabled(),
        "registration_allowed": jwt_enabled() and registration_allowed(),
        # Effective models (a saved override wins over the env var), so the UI
        # header reflects what routing will actually use.
        "models": {
            "router": model_setting("OPENAI_MODEL_ROUTER", "gpt-5-nano"),
            # "" (falsy) means the budget tier is disabled — unlike fast/smart,
            # it has no default; unset = the tier doesn't exist for the UI.
            "budget": model_setting("OPENAI_MODEL_BUDGET", ""),
            "fast": model_setting("OPENAI_MODEL_FAST", base_model),
            "smart": model_setting("OPENAI_MODEL_SMART", base_model),
            "fallback": model_setting("OPENAI_MODEL_FALLBACK", ""),
        },
        # Daily spend cap: only whether a cap is active — live spend figures are
        # withheld from this public, unauthenticated endpoint.
        "budget": budget_status(),
    }
