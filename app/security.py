from __future__ import annotations

import os
import secrets
import time

import bcrypt
from jose import JWTError, jwt

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


def _expire_seconds() -> int:
    raw = (os.getenv("JWT_EXPIRE_MINUTES") or "").strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 60
    if minutes <= 0:
        minutes = 60
    return minutes * 60


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


def decode_token(token: str) -> dict[str, object]:
    """Decode and validate a JWT. Raises JWTError on any problem."""
    return jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])


def subject_from_token(token: str) -> str | None:
    """Return the token's subject if valid and not revoked, else None.

    This is the single chokepoint used by both the API-access guard and the
    conversation-ownership resolver, so a revoked token loses both at once.
    """
    try:
        payload = decode_token(token)
    except JWTError:
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
    except JWTError:
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
