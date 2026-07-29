from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def rate_limiting_enabled() -> bool:
    return bool((os.getenv("RATE_LIMIT") or "").strip())


def rate_limit_value() -> str:
    """
    Limit applied to the expensive ask endpoints, read at request time.

    Uses the standard slowapi syntax, e.g. "60/minute" or "5/second". When
    RATE_LIMIT is unset the limiter is disabled, so this default is only used
    as the decorator's placeholder value.
    """
    return (os.getenv("RATE_LIMIT") or "").strip() or "60/minute"


def _trust_proxy_headers() -> bool:
    raw = (os.getenv("TRUST_PROXY_HEADERS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def client_ip(request: Request) -> str:
    """
    Rate-limit key: the real client IP.

    Behind a trusted reverse proxy (TRUST_PROXY_HEADERS=true — as in the bundled
    docker-compose, where nginx fronts the backend), use the leftmost
    X-Forwarded-For entry so limits are truly per client rather than collapsing
    into one bucket keyed on the proxy's IP. Otherwise use the direct peer
    address. Only enable TRUST_PROXY_HEADERS when a trusted proxy sets the
    header — clients can spoof it if the backend is reachable directly.
    """
    if _trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return get_remote_address(request)


# Keyed by client IP. `enabled` is re-evaluated at startup (see main.lifespan)
# so RATE_LIMIT loaded from .env after import is honored; disabled by default so
# local single-user setups are unaffected.
#
# headers_enabled deliberately stays off: turning it on makes slowapi try to
# inject X-RateLimit-*/Retry-After headers into every response (not just
# 429s), which throws on this slowapi version for any response that isn't a
# plain starlette.responses.Response (e.g. a 401 from a dependency) — worse
# than the missing header it would add.
limiter = Limiter(
    key_func=client_ip,
    enabled=rate_limiting_enabled(),
    default_limits=[],
)


def auth_rate_limit_value() -> str:
    """Per-IP limit for register/login/logout/refresh, e.g. "5/minute"."""
    return (os.getenv("AUTH_RATE_LIMIT") or "").strip() or "5/minute"


# A SECOND limiter, always on — unlike `limiter` above (opt-in via RATE_LIMIT),
# this one is never disabled: a login brute-force or registration-spam vector
# shouldn't depend on an opt-in setting the operator may not have configured.
# It's a separate Limiter instance from `limiter` (each keeps its own request
# counters), so a 429's X-RateLimit-* informational headers are computed off
# whichever instance's storage `app.state.limiter` points at (the ask-endpoint
# `limiter`) rather than this one — cosmetic only; the 429 enforcement itself
# is correct and independent, since each Limiter checks its own counters.
#
# key_style="endpoint" (not slowapi's default "url"): the default keys each
# bucket off the literal resolved request path, which for a path-parameterized
# route (GET /v1/shared/{token}) means every distinct token value gets its own
# bucket — an attacker enumerating tokens would never hit the limit at all,
# silently defeating the entire point of rate-limiting that route. Keying by
# the view function's identity instead means the bucket is shared across every
# value of {token} for the same client IP. No behavior change for the other
# auth_limiter routes (register/login/logout/refresh all have fixed paths, so
# "url" and "endpoint" keys are equivalent there).
auth_limiter = Limiter(key_func=client_ip, default_limits=[], key_style="endpoint")
