"""User accounts: register/login/logout/refresh (unauthenticated — you must
be able to call these without a token yet), and /v1/auth/me + /v1/auth/
change-password (authenticated). Admin user-management (create/reset/
deactivate/reactivate) lives in app/routers/users.py.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from ..auth import _bearer_token, current_owner, is_admin
from ..database import (
    create_user,
    get_user_by_username,
    record_login,
    set_user_password,
)
from ..ratelimit import auth_limiter, auth_rate_limit_value
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from ..security import (
    create_access_token,
    hash_password,
    jwt_enabled,
    registration_allowed,
    revoke_user_sessions,
    rotate_access_token,
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

    username = req.username.strip()
    user = get_user_by_username(username)
    if user is None or not verify_password(req.password, str(user["password_hash"])):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    if not user["is_active"]:
        raise HTTPException(status_code=401, detail="This account is deactivated.")

    record_login(username)
    return TokenResponse(
        access_token=create_access_token(username),
        must_change_password=bool(user["must_change_password"]),
    )


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
    # Validate + revoke + mint in one call, with the replacement carrying the
    # OLD token's epoch claim — see security.rotate_access_token for the
    # logout-vs-refresh race that shape closes.
    fresh = rotate_access_token(token) if token else None
    if fresh is None:
        raise HTTPException(
            status_code=401, detail="Invalid, expired, or revoked token."
        )
    return TokenResponse(access_token=fresh)


@router.get("/v1/auth/me")
def me(owner: str | None = Depends(current_owner)):
    """The current principal: the username when logged in via JWT, else null,
    plus whether it's an admin account and whether it still owes a
    first-sign-in password change."""
    must_change_password = False
    if owner is not None:
        user = get_user_by_username(owner)
        if user is not None:
            must_change_password = bool(user["must_change_password"])
    return {
        "username": owner,
        "is_admin": is_admin(owner),
        "must_change_password": must_change_password,
    }


@router.post("/v1/auth/change-password")
@auth_limiter.limit(auth_rate_limit_value)
def change_password(
    request: Request,
    req: ChangePasswordRequest,
    owner: str | None = Depends(current_owner),
):
    """Set a new password for the logged-in account, clearing
    must_change_password. Works whether or not the flag was set — this is
    also the ordinary "change my password" path, not just the first-sign-in
    flow after an admin create/reset.
    """
    if owner is None:
        raise HTTPException(
            status_code=400, detail="Changing password requires a logged-in account."
        )
    user = get_user_by_username(owner)
    if user is None or not verify_password(
        req.current_password, str(user["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    set_user_password(
        owner, hash_password(req.new_password), must_change_password=False
    )
    return {
        "username": owner,
        "is_admin": is_admin(owner),
        "must_change_password": False,
    }
