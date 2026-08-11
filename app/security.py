"""The cryptography behind authentication: password hashing, JWT minting and
decoding, and the checks that decide whether a presented token still counts.

Paired with app/auth.py, which owns the FastAPI dependencies — the split
keeps every secret-handling decision in one file that no route imports
directly.

A token is valid here only if it survives three separate checks, not one:
the signature and expiry (jwt), then per-token revocation by jti, then the
user's session epoch. The epoch is what makes "log out everywhere" possible
— a token embeds the epoch current when it was issued, so bumping it retires
every token that user holds, including ones already rotated onto a fresh
jti, which per-jti revocation alone can never catch. See app/revocation.py
for where that state lives and what its process-local scope costs.

JWT auth is entirely opt-in: with JWT_SECRET unset, jwt_enabled() is false
and this module's token half is inert, leaving the static-token or no-auth
paths in auth.py.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any

import bcrypt
import jwt
from jwt import PyJWTError

from . import revocation

_ALGORITHM = "HS256"


def jwt_secret() -> str:
    return (os.getenv("JWT_SECRET") or "").strip()


def jwt_enabled() -> bool:
    """JWT user auth is active only when a signing secret is configured."""
    return bool(jwt_secret())


def registration_allowed() -> bool:
    raw = (os.getenv("ALLOW_REGISTRATION") or "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def admin_usernames() -> frozenset[str]:
    """Usernames (case-insensitive) allowed to mutate global settings when
    JWT auth is enabled and registration is open — see main.py's
    `_require_admin`. Comma-separated; empty/unset means none."""
    raw = (os.getenv("ADMIN_USERNAMES") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(name.strip().lower() for name in raw.split(",") if name.strip())


def _expire_seconds() -> int:
    """Access-token lifetime, via JWT_EXPIRY_DAYS (default 30).

    This is a self-hosted personal/family app, not a bank — a long-lived
    session is the right default so signing in once actually sticks;
    JWT_EXPIRE_MINUTES doesn't exist. Existing issued tokens are unaffected
    by a later change to this value (the lifetime is baked into each
    token's own `exp` claim at issue time); only the next sign-in picks up
    a new setting.
    """
    raw = (os.getenv("JWT_EXPIRY_DAYS") or "").strip()
    try:
        days = int(raw)
    except ValueError:
        days = 30
    if days <= 0:
        days = 30
    return days * 86400


def generate_temp_password() -> str:
    """A random one-time temporary password for an admin-created or reset
    account. Returned to the caller exactly once in the API response body —
    never logged, never persisted (only its bcrypt hash is stored)."""
    return secrets.token_urlsafe(12)


def hash_password(password: str) -> str:
    # bcrypt hashes at most 72 bytes; truncate to stay within that limit.
    payload = password.encode("utf-8")[:72]
    return bcrypt.hashpw(payload, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:72], password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + _expire_seconds(),
        # A unique token id so an individual token can be revoked, and the user's
        # current session epoch so "log out everywhere" can invalidate all of
        # their tokens at once (see revocation.py).
        "jti": secrets.token_hex(16),
        "epoch": revocation.user_epoch(username),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises PyJWTError on any problem.

    Claims are typed Any because a JWT payload is arbitrary JSON — callers
    coerce the specific claims they read (str(sub), int(exp), ...).
    """
    return jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])


def subject_from_token(token: str) -> str | None:
    """Return the token's subject if valid and not revoked, else None.

    This is the single chokepoint used by both the API-access guard and the
    conversation-ownership resolver, so a revoked token loses both at once.
    """
    try:
        payload = decode_token(token)
    except PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    jti = payload.get("jti")
    if jti and revocation.is_revoked(str(jti)):
        return None
    # A token issued before the user's current session epoch was logged out.
    if int(payload.get("epoch", 0) or 0) < revocation.user_epoch(str(sub)):
        return None
    return str(sub)


def revoke_token(token: str) -> bool:
    """Revoke a single still-valid token until it would expire (refresh rotation).

    Returns False if the token can't be decoded or lacks a jti/exp.
    """
    try:
        payload = decode_token(token)
    except PyJWTError:
        return False
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return False
    revocation.revoke(str(jti), int(exp))
    return True


def revoke_user_sessions(username: str) -> None:
    """Log a user out everywhere: invalidate all of their existing tokens."""
    revocation.bump_user_epoch(username)
