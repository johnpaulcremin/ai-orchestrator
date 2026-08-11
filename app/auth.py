"""Request-level authentication: the FastAPI dependencies deciding whether a
request may proceed at all, and whose data it then sees.

Deliberately thin, and the split is the point. Everything cryptographic —
password hashing, token minting and decoding, expiry, revocation — lives in
app/security.py; this module only turns an Authorization header into a
yes/no and an owner string, so no route ever parses a header itself.

Two independent questions, two dependencies. require_api_token answers "may
you call this at all": a static shared API_AUTH_TOKEN and per-user JWTs are
both accepted, and with neither configured every request passes (the
single-user local default). current_owner answers "whose conversations are
these": the JWT subject when one is presented, otherwise None, which is the
shared bucket every request lands in when auth is off or a static token was
used. A route that forgets the second one leaks across owners rather than
being merely unauthenticated, which is why conversation-scoped routes all go
through _owned_or_404 rather than filtering by owner themselves.

Admin is a single operator-configured list (ADMIN_USERNAMES), not a role
system, and requires JWT auth to mean anything — a None owner is never an
admin, so a static-token deployment has no admin surface at all.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Depends, Header, HTTPException

from .security import (
    admin_usernames,
    jwt_enabled,
    registration_allowed,
    subject_from_token,
)


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """
    Gate the API behind a bearer credential.

    Two mechanisms, either of which grants access:
      * a static shared token (API_AUTH_TOKEN), and/or
      * a JWT issued by /v1/auth/login (enabled when JWT_SECRET is set).

    When neither is configured, auth is disabled and every request passes.
    """
    static_token = os.getenv("API_AUTH_TOKEN", "").strip()
    jwt_on = jwt_enabled()

    if not static_token and not jwt_on:
        return

    provided = _bearer_token(authorization)
    if provided:
        # Compare as bytes: secrets.compare_digest raises TypeError on non-ASCII
        # str input, and `provided` is attacker-controlled.
        if static_token and secrets.compare_digest(
            provided.encode("utf-8"), static_token.encode("utf-8")
        ):
            return
        if jwt_on and subject_from_token(provided) is not None:
            return

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_owner(authorization: str | None = Header(default=None)) -> str | None:
    """
    The conversation-ownership principal for the request.

    Returns the JWT subject (username) when a valid JWT is presented, else None.
    None means the shared bucket — used when auth is disabled or a static token
    is presented. Access is already gated by require_api_token; this only decides
    *whose* data the request sees.
    """
    if not jwt_enabled():
        return None
    token = _bearer_token(authorization)
    if not token:
        return None
    return subject_from_token(token)


def is_admin(owner: str | None) -> bool:
    """Whether `owner` (a JWT subject) is an operator-configured admin.

    ADMIN_USERNAMES (see security.py) is this app's single admin
    definition, shared by Settings mutation and user-management — there is
    no other admin/role concept. Requires JWT auth to actually be enabled;
    a None owner (auth off, static token, or no ADMIN_USERNAMES set) is
    never an admin.
    """
    if not jwt_enabled() or owner is None:
        return False
    return owner.strip().lower() in admin_usernames()


def require_admin_for_settings(owner: str | None) -> None:
    """Block Settings mutation from a non-admin account.

    Two regimes:
      * ADMIN_USERNAMES is configured: an admin account is required
        regardless of registration state (a multi-user deployment the
        operator has explicitly opted into via ADMIN_USERNAMES).
      * ADMIN_USERNAMES is empty: legacy behavior, unchanged — gate only
        applies when JWT auth is enabled AND registration is open, since
        that's the one combination where an anonymous visitor can
        self-register their own credential and would otherwise inherit the
        same settings-write rights as the operator. Closed registration or
        auth-disabled/static-token deployments are unaffected.
    """
    if admin_usernames():
        if not is_admin(owner):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Settings editing requires an admin account. See ADMIN_USERNAMES."
                ),
            )
        return

    if not jwt_enabled() or not registration_allowed():
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Settings editing requires an admin account while open "
            "registration is enabled. Set ADMIN_USERNAMES, or set "
            "ALLOW_REGISTRATION=false."
        ),
    )


def require_admin_for_users(owner: str | None = Depends(current_owner)) -> str:
    """Gate for user-management endpoints: always requires a real admin
    account, regardless of registration state.

    Unlike require_admin_for_settings, there's no legacy solo behavior to
    preserve here — user management didn't exist before ADMIN_USERNAMES was
    configured, so an empty ADMIN_USERNAMES simply means the feature isn't
    usable yet, not "anything goes."
    """
    if not admin_usernames() or not is_admin(owner):
        raise HTTPException(
            status_code=403,
            detail=(
                "User management requires an admin account. Set "
                "ADMIN_USERNAMES to include this account."
            ),
        )
    assert owner is not None  # guaranteed by is_admin() above
    return owner
