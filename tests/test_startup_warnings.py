"""Startup warnings: auth/rate-limit/budget left at their frictionless-
localhost defaults (see app/main.py's _warn_if_wide_open), and a configured
tier/task-category model whose provider credential isn't set (see
_warn_if_missing_credentials)."""

from __future__ import annotations

import logging

import pytest

from app.main import (
    _warn_if_exposed_without_auth,
    _warn_if_missing_credentials,
    _warn_if_wide_open,
)


def _wide_open_message(caplog: pytest.LogCaptureFixture) -> str | None:
    for record in caplog.records:
        if "startup.wide_open" in record.message:
            return record.message
    return None


def test_warns_about_every_missing_safety_net(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("RATE_LIMIT", raising=False)
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    message = _wide_open_message(caplog)
    assert message is not None
    assert "no auth" in message
    assert "no rate limit" in message
    assert "no daily spend cap" in message


def test_no_warning_when_everything_is_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("RATE_LIMIT", "60/minute")
    monkeypatch.setenv("DAILY_BUDGET_USD", "5")

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    assert _wide_open_message(caplog) is None


def test_jwt_secret_alone_satisfies_the_auth_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("JWT_SECRET", "some-secret")
    monkeypatch.setenv("RATE_LIMIT", "60/minute")
    monkeypatch.setenv("DAILY_BUDGET_USD", "5")

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    assert _wide_open_message(caplog) is None


def test_warns_only_about_the_missing_piece(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret")
    monkeypatch.setenv("RATE_LIMIT", "60/minute")
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    message = _wide_open_message(caplog)
    assert message is not None
    assert "no daily spend cap" in message
    assert "no auth" not in message
    assert "no rate limit" not in message


def test_per_owner_budget_alone_satisfies_the_daily_cap_check(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret")
    monkeypatch.setenv("RATE_LIMIT", "60/minute")
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "5")

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    assert _wide_open_message(caplog) is None


# --- _warn_if_exposed_without_auth -------------------------------------------


def _exposed_message(caplog: pytest.LogCaptureFixture) -> str | None:
    for record in caplog.records:
        if "startup.exposed_without_auth" in record.message:
            return record.message
    return None


def test_warns_when_bound_beyond_localhost_with_no_auth(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIND_HOST", "100.64.1.2")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_exposed_without_auth()

    message = _exposed_message(caplog)
    assert message is not None
    assert "100.64.1.2" in message
    assert "docs/remote-access.md" in message


def test_no_warning_when_bind_host_is_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("BIND_HOST", raising=False)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_exposed_without_auth()

    assert _exposed_message(caplog) is None


@pytest.mark.parametrize("loopback", ["127.0.0.1", "localhost", "::1"])
def test_no_warning_for_a_loopback_bind_host(
    loopback: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIND_HOST", loopback)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_exposed_without_auth()

    assert _exposed_message(caplog) is None


def test_no_warning_when_bound_beyond_localhost_but_static_token_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIND_HOST", "100.64.1.2")
    monkeypatch.setenv("API_AUTH_TOKEN", "secret")
    monkeypatch.delenv("JWT_SECRET", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_exposed_without_auth()

    assert _exposed_message(caplog) is None


def test_no_warning_when_bound_beyond_localhost_but_jwt_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BIND_HOST", "100.64.1.2")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("JWT_SECRET", "some-secret")

    with caplog.at_level(logging.WARNING):
        _warn_if_exposed_without_auth()

    assert _exposed_message(caplog) is None


# --- _warn_if_missing_credentials --------------------------------------------


def _credentials_message(caplog: pytest.LogCaptureFixture) -> str | None:
    for record in caplog.records:
        if "startup.missing_credentials" in record.message:
            return record.message
    return None


def test_warns_about_a_configured_model_with_no_credential(
    db_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "openrouter/meta-llama/llama-3.3-70b")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING):
        _warn_if_missing_credentials()

    message = _credentials_message(caplog)
    assert message is not None
    assert "openrouter/meta-llama/llama-3.3-70b" in message
    assert "OPENROUTER_API_KEY" in message


def test_no_warning_once_the_credential_is_set(
    db_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "openrouter/meta-llama/llama-3.3-70b")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-something")

    with caplog.at_level(logging.WARNING):
        _warn_if_missing_credentials()

    assert _credentials_message(caplog) is None


def test_no_warning_for_ollama_local_models(
    db_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A model whose credential can't be named as a real env var (e.g.
    Ollama's "no API key needed — is it running?" phrase, or Bedrock's "AWS
    credentials") must never be flagged — see settings._key_present, which
    returns None (unknown) rather than False for those, and this function
    only warns on an explicit False."""
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "ollama/llama3.1:8b")

    with caplog.at_level(logging.WARNING):
        _warn_if_missing_credentials()

    assert _credentials_message(caplog) is None


def test_no_warning_when_openai_key_is_set(
    db_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with caplog.at_level(logging.WARNING):
        _warn_if_missing_credentials()

    assert _credentials_message(caplog) is None
