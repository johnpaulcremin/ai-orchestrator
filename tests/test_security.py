from __future__ import annotations

import base64
import json
import time

import pytest
from jose import jwt

from app import security


def test_password_hash_and_verify_roundtrip() -> None:
    hashed = security.hash_password("supersecret")
    assert security.verify_password("supersecret", hashed)
    assert not security.verify_password("wrongpass", hashed)


def test_verify_password_returns_false_on_bad_hash() -> None:
    # Must not raise on a malformed hash.
    assert security.verify_password("x", "not-a-bcrypt-hash") is False


def test_password_truncated_at_72_bytes() -> None:
    base = "a" * 72
    hashed = security.hash_password(base + "EXTRA-tail-1")
    # Passwords sharing the first 72 bytes verify against the same hash.
    assert security.verify_password(base + "DIFFERENT-tail", hashed)


def test_expire_seconds_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "30")
    assert security._expire_seconds() == 1800
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "abc")
    assert security._expire_seconds() == 3600
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "0")
    assert security._expire_seconds() == 3600


def test_token_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "s3cret")
    token = security.create_access_token("alice")
    assert security.subject_from_token(token) == "alice"


def test_expired_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s3cret"
    monkeypatch.setenv("JWT_SECRET", secret)
    past = int(time.time()) - 60
    token = jwt.encode({"sub": "alice", "exp": past}, secret, algorithm="HS256")
    assert security.subject_from_token(token) is None


def test_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "right-secret")
    forged = jwt.encode({"sub": "attacker"}, "wrong-secret", algorithm="HS256")
    assert security.subject_from_token(forged) is None


def test_alg_none_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "right-secret")

    # jose refuses to *encode* alg=none, so craft the raw token by hand — this is
    # the actual attack shape. Decoding it (algorithms pinned to HS256) must fail.
    def b64(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    forged = b64({"alg": "none", "typ": "JWT"}) + "." + b64({"sub": "attacker"}) + "."
    assert security.subject_from_token(forged) is None
