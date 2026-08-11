"""Unauthenticated service-identity endpoints (/, /health, /v1/status) plus
client crash-report intake/review (POST public, GET authed)."""

from __future__ import annotations

import os

from fastapi import Depends, Query, Request
from fastapi.responses import FileResponse

from ..auth import current_owner, require_admin_for_settings
from ..budget import budget_status
from ..database import list_client_errors, record_client_error
from ..frontend_dist import frontend_dist_dir
from ..self_describe import APP_VERSION
from ..ratelimit import auth_limiter, auth_rate_limit_value
from ..routing import tier_output_caps
from ..schemas import ClientErrorReport
from ..security import jwt_enabled, registration_allowed
from ..settings import model_setting
from .deps import public_router, router


@public_router.get("/")
def root(request: Request):
    # When the built frontend is present, serve it here so a single tunnel to
    # this port reaches the whole app (see docs/remote-access.md); otherwise
    # fall back to the plain identity ping this endpoint has always returned.
    index = frontend_dist_dir() / "index.html"
    if index.is_file():
        # See app/security_headers.py: gets the frontend's own CSP instead of
        # the API's default-src 'none'.
        request.state.serves_frontend = True
        return FileResponse(index)
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
        # APP_VERSION, not a literal: this string was hand-maintained here and
        # sat at 0.1.0 through two releases while self_describe reported the
        # truth — the same duplicated-fact drift the codebase inventory exists
        # to prevent, caught by probing this endpoint after the v0.4.0 cut.
        "version": APP_VERSION,
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
        # Each tier's output-token ceiling, so the re-route control can say what
        # headroom an option actually offers instead of implying that any
        # change of tier is a remedy for a cut-off answer. Alongside `models`
        # because it is the same kind of fact — configuration the UI has to
        # describe accurately — and equally unremarkable to expose: these are
        # the app's own limits, not anybody's usage.
        "output_token_caps": tier_output_caps(),
        # Daily spend cap: only whether a cap is active — live spend figures are
        # withheld from this public, unauthenticated endpoint.
        "budget": budget_status(),
    }


@public_router.post("/v1/client-errors", status_code=204)
@auth_limiter.limit(auth_rate_limit_value)
def report_client_error(request: Request, report: ClientErrorReport) -> None:
    """Intake for frontend/src/crashReporter.ts: a browser's own
    window.onerror/onunhandledrejection details, so a device that only shows
    a blank page (devtools out of reach — e.g. a phone) still leaves a
    readable error server-side.

    Deliberately UNAUTHENTICATED (public_router): the report matters most
    precisely when the app crashed before anyone could log in. Hardened the
    same way the other public endpoints are instead — the always-on
    auth_limiter per IP (same guard login/register get), generous transport
    caps on the payload (schemas.ClientErrorReport), tighter
    truncation-on-store caps plus a bounded row count in
    database.record_client_error. User agent comes from the request header,
    not the payload — one less thing to spoof. 204 with no body: the
    reporter is fire-and-forget and never reads the response.

    The per-IP limit is only as strong as client_ip() (app/ratelimit.py):
    behind a proxy that appends X-Forwarded-For with TRUST_PROXY_HEADERS on
    (the bundled docker-compose), the client-supplied leftmost XFF entry is
    spoofable, so a determined attacker can still flood this — the SAME
    caveat that already applies to login/register, not new here. The bounded
    row count (record_client_error prunes to the newest N) is the backstop
    that keeps even an unthrottled flood from growing the database; the
    worst it can do is evict older reports, which is why GET is admin-gated
    (nothing sensitive is exposed) and this stays a debugging aid, not a
    ledger. Tightening client_ip() to use the rightmost/trusted XFF entry is
    a separate, app-wide change (it governs every auth_limiter route).
    """
    record_client_error(
        report.message,
        report.stack,
        report.source_url,
        request.headers.get("user-agent"),
    )


@router.get("/v1/client-errors")
def client_errors(
    owner: str | None = Depends(current_owner),
    limit: int = Query(default=50, ge=1, le=200),
):
    """The stored reports, newest first. Operator-only, not merely authed:
    the table is a single global stream (no owner column — a crash can fire
    before login, so there's nobody to attribute it to) and can contain
    another user's error messages and `source_url`s, including a
    `/shared/{token}` capability URL. `require_api_token` alone (any valid
    JWT) would let a self-registered account read all of that, so this gates
    on admin the same way Settings does — which for the common solo,
    closed-registration deployment is simply "the operator", and which locks
    down the moment open registration could let a stranger self-register.
    """
    require_admin_for_settings(owner)
    return {"errors": list_client_errors(limit)}
