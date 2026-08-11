from __future__ import annotations

import base64
import json
import time

import jwt
import pytest

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


def test_expire_seconds_defaults_to_30_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_EXPIRY_DAYS", raising=False)
    assert security._expire_seconds() == 30 * 86400


def test_expire_seconds_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_EXPIRY_DAYS", "7")
    assert security._expire_seconds() == 7 * 86400
    monkeypatch.setenv("JWT_EXPIRY_DAYS", "abc")
    assert security._expire_seconds() == 30 * 86400
    monkeypatch.setenv("JWT_EXPIRY_DAYS", "0")
    assert security._expire_seconds() == 30 * 86400


def test_expire_seconds_change_does_not_affect_already_issued_tokens(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    # db_path: issuing/decoding a token reads the user's session epoch, which
    # lives in the database now (see app/revocation.py).
    # The lifetime is baked into a token's own `exp` claim at issue time —
    # a later JWT_EXPIRY_DAYS change can't retroactively shorten or lengthen
    # a token that's already out in the world.
    monkeypatch.setenv("JWT_SECRET", "s3cret")
    monkeypatch.setenv("JWT_EXPIRY_DAYS", "30")
    token = security.create_access_token("alice")
    exp_before = security.decode_token(token)["exp"]

    monkeypatch.setenv("JWT_EXPIRY_DAYS", "1")
    assert security.decode_token(token)["exp"] == exp_before
    assert security.subject_from_token(token) == "alice"


def test_token_roundtrip(monkeypatch: pytest.MonkeyPatch, db_path) -> None:
    # db_path: see test_expire_seconds_change_does_not_affect_already_issued_tokens.
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

    # Crafted by hand rather than via jwt.encode(..., algorithm="none") — this
    # is the actual attack shape (a token from an untrusted source, not one
    # this app produced itself). Decoding it (algorithms pinned to HS256 in
    # security.decode_token) must fail regardless of how it was built.
    def b64(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    forged = b64({"alg": "none", "typ": "JWT"}) + "." + b64({"sub": "attacker"}) + "."
    assert security.subject_from_token(forged) is None
