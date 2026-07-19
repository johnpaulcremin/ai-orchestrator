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
limiter = Limiter(
    key_func=client_ip,
    enabled=rate_limiting_enabled(),
    default_limits=[],
)
