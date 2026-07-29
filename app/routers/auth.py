"""User accounts: register/login/logout/refresh (unauthenticated — you must
be able to call these without a token yet) and /v1/auth/me (authenticated).
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from ..auth import _bearer_token, current_owner
from ..database import create_user, get_user_by_username
from ..ratelimit import auth_limiter, auth_rate_limit_value
from ..schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from ..security import (
    create_access_token,
    hash_password,
    jwt_enabled,
    registration_allowed,
    revoke_token,
    revoke_user_sessions,
    subject_from_token,
    verify_password,
)
from .deps import public_router, router


@public_router.post("/v1/auth/register", response_model=UserOut, status_code=201)
@auth_limiter.limit(auth_rate_limit_value)
def register(request: Request, req: RegisterRequest):
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )
    if not registration_allowed():
        raise HTTPException(status_code=403, detail="Registration is disabled.")

    user = create_user(req.username.strip(), hash_password(req.password))
    if user is None:
        raise HTTPException(status_code=409, detail="Username already exists.")

    return user


@public_router.post("/v1/auth/login", response_model=TokenResponse)
@auth_limiter.limit(auth_rate_limit_value)
def login(request: Request, req: LoginRequest):
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )

    user = get_user_by_username(req.username.strip())
    if user is None or not verify_password(req.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    return TokenResponse(access_token=create_access_token(req.username.strip()))


def _require_jwt_enabled() -> None:
    if not jwt_enabled():
        raise HTTPException(
            status_code=400, detail="JWT auth is not enabled (set JWT_SECRET)."
        )


@public_router.post("/v1/auth/logout")
@auth_limiter.limit(auth_rate_limit_value)
def logout(request: Request, authorization: str | None = Header(default=None)):
    """Log the user out everywhere: invalidate all of their existing tokens.

    Bumping the user's session epoch also kills any token that was refreshed onto
    a fresh jti, so a compromised session can't outlive a logout.
    """
    _require_jwt_enabled()
    token = _bearer_token(authorization)
    subject = subject_from_token(token) if token else None
    if subject is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token.")
    revoke_user_sessions(subject)
    return {"status": "logged_out"}


@public_router.post("/v1/auth/refresh", response_model=TokenResponse)
@auth_limiter.limit(auth_rate_limit_value)
def refresh(request: Request, authorization: str | None = Header(default=None)):
    """Trade a still-valid, non-revoked token for a fresh one, rotating it.

    The presented token is revoked, so a leaked token can't be replayed after the
    holder refreshes.
    """
    _require_jwt_enabled()
    token = _bearer_token(authorization)
    subject = subject_from_token(token) if token else None
    if subject is None:
        raise HTTPException(
            status_code=401, detail="Invalid, expired, or revoked token."
        )
    revoke_token(token)  # rotate: the old token stops working immediately
    return TokenResponse(access_token=create_access_token(subject))


@router.get("/v1/auth/me")
def me(owner: str | None = Depends(current_owner)):
    """The current principal: the username when logged in via JWT, else null."""
    return {"username": owner}
