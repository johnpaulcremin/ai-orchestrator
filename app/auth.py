from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """
    Optional bearer-token auth for the API.

    API_AUTH_TOKEN is read at request time. When it is empty or unset, auth is
    disabled and every request passes. When it is set, requests must send
    "Authorization: Bearer <token>" with a matching token.
    """
    expected = os.getenv("API_AUTH_TOKEN", "").strip()
    if not expected:
        return

    provided = ""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.strip().lower() == "bearer":
            provided = token.strip()

    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
