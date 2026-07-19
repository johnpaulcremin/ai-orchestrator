from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException

from .security import jwt_enabled, subject_from_token


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
