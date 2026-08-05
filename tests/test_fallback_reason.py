"""app/fallback_reason.py: classifying WHY a primary model call failed and
needed a fallback. See that module's docstring for the six reason
categories and why BUDGET_REFUSAL is never classified FROM an exception.
"""

from __future__ import annotations

import anthropic
import httpx
import openai
import pytest

from app import database
from app import fallback_reason as fr


def _column_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def test_fresh_db_has_the_fallback_log_table_and_index(db_path) -> None:
    columns = _column_names(db_path, "fallback_log")
    assert columns == {"id", "owner", "model", "reason", "succeeded", "created_at"}
    assert "idx_fallback_log_created_at" in _index_names(db_path, "fallback_log")


def test_record_and_query_fallback_events(db_path) -> None:
    database.record_fallback_event("alice", "gpt-5", fr.TIMEOUT, succeeded=True)
    database.record_fallback_event("alice", "gpt-5", fr.TIMEOUT, succeeded=True)
    database.record_fallback_event(
        "alice", "claude-sonnet-5", fr.BUDGET_REFUSAL, succeeded=False
    )

    counts = database.fallback_reason_counts("alice", days=1)
    assert counts == [
        {"reason": "timeout", "count": 2},
        {"reason": "budget_refusal", "count": 1},
    ]


def test_fallback_reason_counts_is_owner_scoped(db_path) -> None:
    database.record_fallback_event("alice", "gpt-5", fr.TIMEOUT, succeeded=True)
    assert database.fallback_reason_counts("bob", days=1) == []


def test_fallback_reason_counts_respects_the_days_window(db_path) -> None:
    import sqlite3

    database.record_fallback_event("alice", "gpt-5", fr.TIMEOUT, succeeded=True)
    conn = sqlite3.connect(database._db_path())
    conn.execute("UPDATE fallback_log SET created_at = datetime('now', '-10 days')")
    conn.commit()
    conn.close()

    assert database.fallback_reason_counts("alice", days=1) == []


def _openai_bad_request(code: str | None, message: str) -> openai.BadRequestError:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(400, request=req)
    body: dict[str, object] = {"message": message}
    if code is not None:
        body["code"] = code
    return openai.BadRequestError(message, response=resp, body=body)


def _anthropic_bad_request(message: str) -> anthropic.BadRequestError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(400, request=req)
    return anthropic.BadRequestError(message, response=resp, body={"message": message})


def _openai_timeout() -> openai.APITimeoutError:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APITimeoutError(request=req)


def _anthropic_timeout() -> anthropic.APITimeoutError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APITimeoutError(request=req)


def _openai_rate_limit() -> openai.RateLimitError:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(429, request=req)
    return openai.RateLimitError("rate limited", response=resp, body=None)


def _anthropic_rate_limit() -> anthropic.RateLimitError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req)
    return anthropic.RateLimitError("rate limited", response=resp, body=None)


def _openai_connection_error() -> openai.APIConnectionError:
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIConnectionError(message="connection refused", request=req)


def _anthropic_connection_error() -> anthropic.APIConnectionError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(message="connection refused", request=req)


class _FakeLitellmContextWindowExceededError(Exception):
    """Stands in for litellm.exceptions.ContextWindowExceededError — the
    classifier matches by class NAME (see module docstring), so a fake with
    the same name proves that path without importing litellm in tests."""


_FakeLitellmContextWindowExceededError.__name__ = "ContextWindowExceededError"


# --- timeout / quota-cooldown: exception TYPE, highest priority -------------


def test_openai_timeout_is_timeout() -> None:
    assert fr.classify_error_reason(_openai_timeout()) == fr.TIMEOUT


def test_anthropic_timeout_is_timeout() -> None:
    assert fr.classify_error_reason(_anthropic_timeout()) == fr.TIMEOUT


def test_openai_rate_limit_is_quota_cooldown() -> None:
    assert fr.classify_error_reason(_openai_rate_limit()) == fr.QUOTA_COOLDOWN


def test_anthropic_rate_limit_is_quota_cooldown() -> None:
    assert fr.classify_error_reason(_anthropic_rate_limit()) == fr.QUOTA_COOLDOWN


# --- connection errors (must not be misread as a timeout) -------------------


def test_openai_connection_error_is_connection_error() -> None:
    assert fr.classify_error_reason(_openai_connection_error()) == fr.CONNECTION_ERROR


def test_anthropic_connection_error_is_connection_error() -> None:
    assert (
        fr.classify_error_reason(_anthropic_connection_error()) == fr.CONNECTION_ERROR
    )


def test_timeout_is_not_misclassified_as_connection_error() -> None:
    """openai.APITimeoutError IS-A openai.APIConnectionError — the TIMEOUT
    check must run first so a real timeout is never reported as a generic
    connection error."""
    assert fr.classify_error_reason(_openai_timeout()) == fr.TIMEOUT


# --- litellm's own ContextWindowExceededError, matched by class name --------


def test_litellm_context_window_exceeded_by_class_name() -> None:
    assert (
        fr.classify_error_reason(_FakeLitellmContextWindowExceededError("too big"))
        == fr.CONTEXT_LENGTH_EXCEEDED
    )


# --- BadRequestError: keyword-sniffed sub-categories -------------------------


@pytest.mark.parametrize(
    "code,message",
    [
        ("context_length_exceeded", "This model's maximum context length is 8192"),
        (None, "Your prompt is too long for this model's context window"),
        (None, "maximum context length exceeded"),
    ],
)
def test_openai_bad_request_context_length(code: str | None, message: str) -> None:
    assert (
        fr.classify_error_reason(_openai_bad_request(code, message))
        == fr.CONTEXT_LENGTH_EXCEEDED
    )


def test_anthropic_bad_request_context_length() -> None:
    error = _anthropic_bad_request("prompt is too long: 250000 tokens > 200000 max")
    assert fr.classify_error_reason(error) == fr.CONTEXT_LENGTH_EXCEEDED


@pytest.mark.parametrize(
    "message",
    [
        "This model does not support tools",
        "Function calling is not supported for this model",
        "tool_choice is not supported with this model",
    ],
)
def test_bad_request_tool_unsupported(message: str) -> None:
    error = _openai_bad_request(None, message)
    assert fr.classify_error_reason(error) == fr.TOOL_UNSUPPORTED


def test_bad_request_unrecognized_falls_back_to_provider_error() -> None:
    """Errs toward the generic bucket over guessing a wrong specific label
    — same posture as the FACT_CHECK/SELF_DESCRIBE phrase lists."""
    error = _openai_bad_request(None, "The 'temperature' field must be between 0 and 2")
    assert fr.classify_error_reason(error) == fr.PROVIDER_ERROR


# --- catch-all -------------------------------------------------------------


def test_unrecognized_exception_is_provider_error() -> None:
    assert fr.classify_error_reason(RuntimeError("something else broke")) == (
        fr.PROVIDER_ERROR
    )


# --- labels / constants -----------------------------------------------------


def test_every_reason_has_a_label() -> None:
    assert set(fr.REASON_LABELS) == set(fr.ALL_REASONS)


def test_budget_refusal_label_matches_the_spec_wording() -> None:
    assert fr.REASON_LABELS[fr.BUDGET_REFUSAL] == "budget refusal"


def test_classify_never_returns_budget_refusal() -> None:
    """BUDGET_REFUSAL is only ever assigned by the caller (orchestrator.py's
    fallback loop) when every candidate was skipped for budget reasons —
    never derived from an exception (see module docstring)."""
    samples = [
        _openai_timeout(),
        _anthropic_timeout(),
        _openai_rate_limit(),
        _anthropic_rate_limit(),
        _openai_connection_error(),
        _anthropic_connection_error(),
        _FakeLitellmContextWindowExceededError("too big"),
        _openai_bad_request("context_length_exceeded", "too long"),
        _openai_bad_request(None, "does not support tools"),
        RuntimeError("anything"),
    ]
    assert all(fr.classify_error_reason(s) != fr.BUDGET_REFUSAL for s in samples)
