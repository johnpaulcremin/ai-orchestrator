from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from .budget import daily_budget_per_owner_usd, daily_budget_usd
from .database import init_db
from .observability import setup_tracing
from .ratelimit import limiter, rate_limiting_enabled
from .security import jwt_enabled
from .security_headers import SecurityHeadersMiddleware
from .settings import describe_settings
from .telemetry import logger

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def _warn_if_wide_open() -> None:
    """Log one loud, consolidated warning for each safety net left off.

    All of these default to "off" for a frictionless localhost dev run, which
    is the right default for that case — but the exact same defaults, copied
    straight into a Docker/internet-facing deployment (docker-compose.yml
    binds 0.0.0.0), leave the API unauthenticated, unrated, and uncapped. This
    can't stop that deployment, but it makes sure the operator can't miss it.
    """
    off = []
    if not os.getenv("API_AUTH_TOKEN", "").strip() and not jwt_enabled():
        off.append("no auth (API_AUTH_TOKEN and JWT_SECRET are both unset)")
    if not rate_limiting_enabled():
        off.append("no rate limit on ask endpoints (RATE_LIMIT is unset)")
    if daily_budget_usd() is None and daily_budget_per_owner_usd() is None:
        off.append(
            "no daily spend cap (DAILY_BUDGET_USD and DAILY_BUDGET_PER_OWNER_USD are both unset)"
        )
    if not off:
        return
    logger.warning(
        "startup.wide_open — running with: %s. Fine for local dev; before "
        "exposing this beyond localhost, set at least API_AUTH_TOKEN or "
        "JWT_SECRET. See the README's Security/Deployment guidance.",
        "; ".join(off),
    )


def _warn_if_exposed_without_auth() -> None:
    """Log a loud, specific warning when this process has been told it's
    bound beyond localhost (via BIND_HOST — see docs/remote-access.md) and
    neither auth mechanism is configured. Distinct from _warn_if_wide_open
    above: that one fires for the ordinary Docker/local-dev case regardless
    of bind address (RATE_LIMIT/DAILY_BUDGET_USD unset is a soft nudge even
    on localhost); this one is specifically about the remote-access
    scenario docs/remote-access.md walks through (e.g. a Tailscale IP), where
    an unauthenticated API is reachable from another device entirely — a
    materially different risk, so it gets its own message pointing at that
    doc rather than the README.

    uvicorn's `--host` CLI flag isn't introspectable from inside the ASGI
    app, so this relies on BIND_HOST being set to the same value passed to
    `--host` — an operator opts into the signal by setting it, same as any
    other env-var-driven flag in this app. Unset (the default local-dev
    case) never warns.
    """
    bind_host = (os.getenv("BIND_HOST") or "").strip()
    if not bind_host or bind_host in {"127.0.0.1", "localhost", "::1"}:
        return
    if os.getenv("API_AUTH_TOKEN", "").strip() or jwt_enabled():
        return
    logger.warning(
        "startup.exposed_without_auth — bound to %s (BIND_HOST) with no "
        "auth configured (API_AUTH_TOKEN and JWT_SECRET are both unset). "
        "Anyone who can reach this address can use the app and spend your "
        "budget. See docs/remote-access.md before exposing this beyond "
        "localhost.",
        bind_host,
    )


def _warn_if_missing_credentials() -> None:
    """Log one warning naming every configured tier/task-category model
    whose provider credential isn't set — e.g. an OPENAI_MODEL_FAST (or
    MODEL_<CATEGORY>) pointed at an openrouter/... or any other
    LiteLLM-routed model without OPENROUTER_API_KEY (or that provider's own
    key env var) set. Reuses the exact same key_env_for/key-presence check
    the Settings panel already surfaces per model (see
    settings.describe_settings/_credential_info) — this just makes the same
    signal visible at boot, without opening Settings, since a misconfigured
    credential otherwise only shows up as a failed answer on first use.
    """
    described = describe_settings()
    missing: set[str] = set()
    for entry in (*described["tiers"], *described["categories"]):
        if entry.get("key_present") is False and entry.get("effective_model"):
            missing.add(f"{entry['effective_model']} (needs {entry['key_env']})")
    if not missing:
        return
    logger.warning(
        "startup.missing_credentials — %d configured model(s) have no "
        "credential set: %s",
        len(missing),
        "; ".join(sorted(missing)),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    setup_tracing(app)
    # Re-evaluate now that .env is loaded (the limiter was constructed at import,
    # possibly before load_dotenv ran).
    limiter.enabled = rate_limiting_enabled()
    _warn_if_wide_open()
    _warn_if_exposed_without_auth()
    _warn_if_missing_credentials()
    yield


app = FastAPI(
    title="AI Orchestrator API",
    version="0.1.0",
    lifespan=lifespan,
)

# Rate limiting (opt-in via RATE_LIMIT). Registered even when disabled so the
# decorators on the ask endpoints resolve; the limiter no-ops when disabled.
app.state.limiter = limiter
# slowapi's handler is typed (Request, RateLimitExceeded) -> Response, narrower
# than Starlette's (Request, Exception) protocol, so mypy flags the variance.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


def _allowed_origins() -> list[str]:
    raw = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)
# Registered after CORSMiddleware, which — since Starlette applies
# middleware in reverse-registration order — means this one actually runs
# FIRST on the response path, so its headers are already on the response by
# the time CORSMiddleware adds its own; either order is fine here since
# they touch disjoint header names, but this keeps the security headers as
# close to "always present, nothing skips them" as the middleware stack
# allows.
app.add_middleware(SecurityHeadersMiddleware)

# Importing each domain module registers its routes onto the shared
# `router`/`public_router` instances from app.routers.deps — imported here
# purely for that side effect, so every route exists before either shared
# router is included below.
from .routers import (  # noqa: E402,F401
    ask,
    auth,
    compat,
    conversations,
    library,
    media,
    messages,
    settings,
    shares,
    system,
    templates,
    usage,
)
from .routers.deps import public_router, router  # noqa: E402

app.include_router(public_router)
app.include_router(router)
