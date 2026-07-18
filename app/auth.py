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
        if static_token and secrets.compare_digest(provided, static_token):
            return
        if jwt_on and subject_from_token(provided) is not None:
            return

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
