"""The FastAPI application itself: startup checks, middleware, the router
table, and serving the built frontend.

Almost no business logic lives here — the endpoints are in app/routers/* and
the work behind them in the modules those import. What this module owns is
everything that has to happen once, around all of them.

The startup warnings are the substance. Each one names a misconfiguration
that is silent at boot and expensive later: a deployment reachable from
outside with no auth configured, a model pointed at a provider whose
credential is missing, a local endpoint that is not answering. Every check
warns and continues rather than refusing to start — a single unreachable
Ollama model should not take down an app that can still answer through four
other providers — so the operator learns at boot instead of on the first
request that fails.

The frontend is served from the same origin when a build is present, which
is what lets the whole app run behind one port with no CORS story at all;
the /api prefix rewrite exists so the same frontend bundle works both that
way and against a separate dev server on 5173.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.types import ASGIApp

from . import image_processing, local_endpoints, local_health
from .budget import daily_budget_per_owner_usd, daily_budget_usd
from .database import init_db
from .frontend_dist import frontend_dist_dir
from .observability import setup_tracing
from .ratelimit import limiter, rate_limiting_enabled
from .security import jwt_enabled
from .security_headers import SecurityHeadersMiddleware
from .settings import bool_setting, describe_settings, get_model_overrides
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


def _warn_if_local_model_unreachable() -> None:
    """Log a warning for every configured LOCAL model whose server this
    process can't actually open a socket to.

    The gap _warn_if_missing_credentials structurally cannot cover: a local
    model has no credential, so "key_present" is never False for one. Yet an
    unreachable local model is the more expensive misconfiguration of the two
    — it doesn't fail the request, it silently promotes every call on that
    tier to a PAID fallback model (see app/local_health.py for the real case
    this was written for, where a "budget" tier billed gpt-5 prices for weeks
    while Ollama itself was up and healthy the whole time).

    Best-effort and never fatal: a probe failure at boot is a warning, not a
    reason to refuse to start — the operator may be about to start the server.
    """
    described = describe_settings()
    configured = {
        str(entry["effective_model"])
        for entry in (*described["tiers"], *described["categories"])
        if entry.get("effective_model")
    }

    checked: dict[str, bool] = {}  # base_url -> reachable, probed once each
    unreachable: list[str] = []
    for model in sorted(configured):
        prefix = model.split("/", 1)[0].strip().lower()
        if local_endpoints.is_local_endpoint_model(model):
            base_url = local_endpoints.base_url_for(model)
        elif prefix in {"ollama", "ollama_chat"}:
            base_url = local_health.ollama_base_url()
        else:
            continue
        if not base_url:
            continue
        if base_url not in checked:
            checked[base_url] = local_health.is_reachable(base_url)
        if checked[base_url]:
            continue
        container_host = local_health.container_only_host(base_url)
        hint = (
            f" — '{container_host}' only resolves inside a container; use "
            "'localhost' when running this app directly on the host"
            if container_host
            else " — is that server running?"
        )
        unreachable.append(f"{model} at {base_url}{hint}")

    if not unreachable:
        return
    logger.warning(
        "startup.local_model_unreachable — %d configured local model(s) can't "
        "be reached from this process, so every call routed to one will fail "
        "over to a PAID model and silently cost money: %s",
        len(unreachable),
        "; ".join(unreachable),
    )


def _warn_if_ocr_unavailable() -> None:
    """Log a warning when OCR_REPLACEMENT is on but Tesseract isn't installed.

    Same shape as _warn_if_local_model_unreachable, and the same reason: a
    feature the owner has switched ON that cannot possibly work, failing in
    silence. image_processing._tesseract_available() returns False, caches
    that for the life of the process, and every ocr_extract() call returns
    None — no log line, no error, nothing on the answer. Meanwhile
    self_describe reports OCR_REPLACEMENT under "Enabled optional features",
    so asked about it the app will confirm the feature is on.

    Only fires when OCR_REPLACEMENT was set EXPLICITLY — an env var or a
    saved Settings override. It defaults to ON (see
    image_processing._ocr_enabled), and Tesseract is an optional system
    binary most installs do not have, so warning on the default would fire
    on the majority of fresh installs about a graceful degradation nobody
    asked for. That is boot noise, and boot noise is how a real warning gets
    ignored. The same rule the local-model probe follows implicitly: it can
    only warn about a model somebody configured.

    Cheaper than the model probe (an import plus a version call, once at
    boot, cached from then on) and never fatal: the operator may be about to
    install it.
    """
    overrides = get_model_overrides()
    explicit = (overrides.get("OCR_REPLACEMENT") or "").strip() or (
        os.getenv("OCR_REPLACEMENT") or ""
    ).strip()
    if not explicit:
        return
    # Same overrides map the explicit check just read: fetching it twice
    # could see two different answers if Settings were saved in between.
    if not bool_setting("OCR_REPLACEMENT", False, overrides):
        return
    if image_processing.tesseract_available():
        return
    configured = (os.getenv("TESSERACT_CMD") or "").strip()
    hint = (
        f" — TESSERACT_CMD points at '{configured}'"
        if configured
        else " — install Tesseract, or set TESSERACT_CMD to its binary"
    )
    logger.warning(
        "startup.ocr_unavailable — OCR_REPLACEMENT is ON but the Tesseract "
        "binary can't be reached from this process, so every attached image "
        "is sent as an IMAGE and the feature does nothing at all%s",
        hint,
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
    _warn_if_local_model_unreachable()
    _warn_if_ocr_unavailable()
    yield


app = FastAPI(
    title="AI Orchestrator API",
    version="0.1.0",
    lifespan=lifespan,
)


class _ApiPrefixRewriteMiddleware:
    """Strip a leading /api so frontend fetches reach their real route.

    The frontend's fetch client (frontend/src/App.tsx's `API_BASE = "/api"`)
    assumes something in front of this backend strips that prefix before the
    request arrives — true for both the Vite dev proxy (vite.config.ts) and
    the Docker nginx deploy (frontend/nginx.conf's `location /api/` block).
    Now that this backend also serves the built frontend directly (see
    docs/remote-access.md), there's no such proxy in front of it for that
    case, so it does the same rewrite itself. Plain ASGI middleware (not
    BaseHTTPMiddleware) so it can rewrite `scope["path"]` before routing
    ever sees it, rather than after a response is already produced.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                scope = dict(scope)
                scope["path"] = path[len("/api") :] or "/"
        await self.app(scope, receive, send)


app.add_middleware(_ApiPrefixRewriteMiddleware)

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
    users,
)
from .routers.deps import public_router, router  # noqa: E402

app.include_router(public_router)
app.include_router(router)


# Registered last so every explicit route above (including this module's own
# "/", /health, /v1/*, /docs, /openapi.json) matches first and is unaffected;
# this only catches GET paths nothing else claimed. When frontend/dist exists
# it serves the built SPA's static assets and falls back to index.html for
# client-side routes (see docs/remote-access.md); when it doesn't, it 404s,
# identical to the pre-existing behavior for an unmatched path.
@app.get("/{full_path:path}", include_in_schema=False)
async def _frontend_spa(request: Request, full_path: str) -> FileResponse:
    dist = frontend_dist_dir()
    index = dist / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404)
    candidate = (dist / full_path).resolve()
    try:
        candidate.relative_to(dist.resolve())
    except ValueError:
        raise HTTPException(status_code=404)
    # See app/security_headers.py: this flag gets the response the frontend's
    # own CSP instead of the API's default-src 'none' (which would otherwise
    # block the app's own scripts/styles and render a blank page).
    request.state.serves_frontend = True
    # Explicit cache policy, split by what the filename promises. Without a
    # Cache-Control header browsers fall back to HEURISTIC caching, and iOS
    # Safari (especially an installed-to-home-screen PWA) served a stale
    # index.html for hours after a rebuild — one still referencing the OLD
    # hashed bundle, so a freshly deployed frontend "didn't change" on the
    # phone. Observed live through the tailscale tunnel. Vite content-hashes
    # everything under /assets/, so those are immutable by construction and
    # may cache forever; index.html is the one mutable entry point, so it
    # must revalidate every load. (Plain FileResponse does not implement
    # conditional requests, so revalidation refetches the full body — the
    # shell is ~1.2KB, and the hashed assets it references never refetch,
    # so that is the whole recurring cost of always-fresh deploys.)
    if full_path and candidate.is_file():
        response = FileResponse(candidate)
        if full_path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # favicon/manifest and friends: mutable names, same revalidate
            # rule as the entry point.
            response.headers["Cache-Control"] = "no-cache"
        return response
    if full_path.startswith("assets/"):
        # A MISSING asset is a dead reference (most likely a stale cached
        # shell asking for a bundle a rebuild replaced), never a client-side
        # route — falling through to index.html here served a text/html body
        # where a browser expected CSS/JS, turning "stale cache" into
        # confusing half-styled breakage instead of a clean 404. Found by
        # probing an old bundle name through the live tunnel.
        raise HTTPException(status_code=404)
    response = FileResponse(index)
    response.headers["Cache-Control"] = "no-cache"
    return response
