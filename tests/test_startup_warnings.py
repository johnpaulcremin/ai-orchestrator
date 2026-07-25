"""Startup warning when auth/rate-limit/budget are all left at their
frictionless-localhost defaults (see app/main.py's _warn_if_wide_open)."""

from __future__ import annotations

import logging

import pytest

from app.main import _warn_if_wide_open


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

    with caplog.at_level(logging.WARNING):
        _warn_if_wide_open()

    message = _wide_open_message(caplog)
    assert message is not None
    assert "no daily spend cap" in message
    assert "no auth" not in message
    assert "no rate limit" not in message
