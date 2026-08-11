"""Rate limiting, in two independent limiters with different defaults.

`limiter` guards the expensive endpoints (ask, speak, transcribe) and is
OFF unless RATE_LIMIT is set — a local single-user install should not be
throttled by default. `auth_limiter` guards login, registration and the
unauthenticated crash-report intake, and is always on: those are reachable
without a credential, so brute-force protection cannot be something an
operator has to remember to enable.

The rate-limit key is the client IP, which is only correct behind a proxy if
the proxy is trusted. TRUST_PROXY_HEADERS makes client_ip() read the
leftmost X-Forwarded-For entry (right for the bundled nginx compose setup,
where every request would otherwise collapse into one bucket keyed on the
proxy); leaving it off uses the direct peer address. Enabling it when the
backend is also reachable directly lets a client spoof the header and its
own limit away, so the default is the safe one.

Both limits are read at request time rather than baked into the decorator,
so a value loaded from .env after import is honoured. headers_enabled stays
off deliberately: on this slowapi version, injecting X-RateLimit-* headers
throws for any response that is not a plain starlette Response — such as a
401 raised by a dependency — which is a worse failure than the missing
header it would add.
"""

from __future__ import annotations

import os

import jwt
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


def refresh_account_key(request: Request) -> str:
    """Rate-limit key for /v1/auth/refresh: the presented token's CLAIMED
    subject, falling back to the client IP when there is no decodable claim.

    Exists because the per-IP bucket alone did not bound the one durable
    thing refresh does — inserting a revoked_tokens row that persists for the
    old token's full remaining lifetime. Under TRUST_PROXY_HEADERS with a
    directly reachable backend (a misconfiguration this module's own
    docstring warns about), X-Forwarded-For is spoofable, so a single
    authenticated attacker could take a fresh per-IP bucket per request and
    insert rows at server speed. Keyed by account, the bucket cannot be
    rotated: a row is only ever inserted for a VALID token, and a valid
    token's subject is fixed.

    The claim is read WITHOUT signature verification, and that is sound for
    this purpose precisely because of the line above: a forged token can
    claim any subject it likes and thereby pick its bucket, but every such
    request dies at 401 without touching the database — and it additionally
    remains inside the ordinary per-IP bucket, which still applies to the
    same route. Never used for anything but choosing a bucket name.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.strip().lower() == "bearer" and token.strip():
        try:
            payload = jwt.decode(
                token.strip(),
                options={"verify_signature": False, "verify_exp": False},
            )
            sub = str(payload.get("sub") or "").strip()
            if sub:
                return f"account:{sub}"
        except jwt.PyJWTError:
            pass
    return client_ip(request)


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
