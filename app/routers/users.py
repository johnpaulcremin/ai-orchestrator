"""Admin-only user management: list/create/reset-password/deactivate/
reactivate accounts. Every endpoint here requires an admin account (see
app/auth.py's require_admin_for_users) regardless of registration state —
this capability doesn't exist at all until the operator sets
ADMIN_USERNAMES.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException

from ..auth import require_admin_for_users
from ..database import (
    create_user,
    list_users,
    set_user_active,
    set_user_password,
)
from ..schemas import (
    AdminCreateUserRequest,
    AdminCreateUserResponse,
    AdminUserOut,
    ResetPasswordResponse,
)
from ..security import generate_temp_password, hash_password, revoke_user_sessions
from .deps import router


@router.get("/v1/users", response_model=list[AdminUserOut])
def list_all_users(admin: str = Depends(require_admin_for_users)):
    """Every account, newest first."""
    return list_users()


@router.post("/v1/users", response_model=AdminCreateUserResponse, status_code=201)
def create_admin_user(
    req: AdminCreateUserRequest, admin: str = Depends(require_admin_for_users)
):
    """Create an account with a random temporary password, flagged
    must_change_password. The password is returned exactly once, here."""
    temp_password = generate_temp_password()
    user = create_user(
        req.username.strip(), hash_password(temp_password), must_change_password=True
    )
    if user is None:
        raise HTTPException(status_code=409, detail="Username already exists.")
    return AdminCreateUserResponse(
        user=AdminUserOut(**user), temporary_password=temp_password
    )


@router.post(
    "/v1/users/{username}/reset-password", response_model=ResetPasswordResponse
)
def reset_user_password(username: str, admin: str = Depends(require_admin_for_users)):
    """Set a new random temporary password for `username`, flagging
    must_change_password so the next sign-in forces a real one. Returned
    exactly once, here; existing sessions are revoked so a leaked/forgotten
    old password can't keep working."""
    temp_password = generate_temp_password()
    updated = set_user_password(
        username, hash_password(temp_password), must_change_password=True
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found.")
    revoke_user_sessions(username)
    return ResetPasswordResponse(temporary_password=temp_password)


@router.post("/v1/users/{username}/deactivate", response_model=AdminUserOut)
def deactivate_user(username: str, admin: str = Depends(require_admin_for_users)):
    """Deactivate an account: it can no longer sign in, its outstanding
    sessions are revoked immediately, but its conversations are untouched
    and reappear as-is on reactivation."""
    updated = set_user_active(username, False)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    revoke_user_sessions(username)
    return AdminUserOut(**updated)


@router.post("/v1/users/{username}/reactivate", response_model=AdminUserOut)
def reactivate_user(username: str, admin: str = Depends(require_admin_for_users)):
    """Restore a deactivated account's ability to sign in."""
    updated = set_user_active(username, True)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return AdminUserOut(**updated)
