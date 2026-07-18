from __future__ import annotations

import os
import time

import bcrypt
from jose import JWTError, jwt

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
    }
    return jwt.encode(payload, jwt_secret(), algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, object]:
    """Decode and validate a JWT. Raises JWTError on any problem."""
    return jwt.decode(token, jwt_secret(), algorithms=[_ALGORITHM])


def subject_from_token(token: str) -> str | None:
    """Return the token's subject if valid, else None."""
    try:
        payload = decode_token(token)
    except JWTError:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None
