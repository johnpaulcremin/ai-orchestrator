"""First-run setup (app/routers/setup.py): verify a candidate API key with one
minimal call and classify the outcome — never storing, logging, or echoing
the key — plus the /v1/status field the wizard keys off.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.routers import setup

SECRET = "sk-test-DO-NOT-LEAK-0123456789"


class _FakeAuthError(Exception):
    pass


class _FakeRateError(Exception):
    pass


class _FakeTimeout(Exception):
    pass


class _FakeConnectionError(Exception):
    pass


class _FakeBadRequest(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _fake_openai(monkeypatch: pytest.MonkeyPatch, create):
    """Install a throwaway OpenAI client whose responses.create is `create`,
    recording the api_key it was constructed with. Exception classes are
    swapped for local ones so a test never has to build the SDK's own error
    objects (which want a real httpx response)."""
    seen: dict = {}

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            seen["api_key"] = api_key
            self.responses = SimpleNamespace(create=create)

        def with_options(self, **_kw):
            return self

    monkeypatch.setattr(setup, "OpenAI", FakeClient)
    monkeypatch.setattr(setup, "AUTH_ERRORS", (_FakeAuthError,))
    monkeypatch.setattr(setup, "RATE_ERRORS", (_FakeRateError,))
    monkeypatch.setattr(setup, "TIMEOUT_ERRORS", (_FakeTimeout,))
    monkeypatch.setattr(setup, "APIConnectionError", _FakeConnectionError)
    monkeypatch.setattr(setup, "BadRequestError", _FakeBadRequest)
    return seen


def _test_key(client: TestClient, key: str = SECRET):
    return client.post("/v1/setup/test-key", json={"api_key": key})


# --- outcomes -------------------------------------------------------------------


def test_working_key_is_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_openai(monkeypatch, lambda **_kw: SimpleNamespace(output_text="pong"))
    res = _test_key(client)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["outcome"] == "ok"
    assert body["key_env"] == "OPENAI_API_KEY"
    # The candidate key, not the ambient one, is what gets tested.
    assert seen["api_key"] == SECRET


def test_rejected_key_is_auth_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_kw):
        raise _FakeAuthError("401")

    _fake_openai(monkeypatch, refuse)
    body = _test_key(client).json()
    assert body["ok"] is False
    assert body["outcome"] == "auth_failed"


def test_throttled_key_still_counts_as_working(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 means the credential was accepted and the account is busy — the
    only thing being tested has already passed."""

    def throttle(**_kw):
        raise _FakeRateError("429")

    _fake_openai(monkeypatch, throttle)
    body = _test_key(client).json()
    assert body["ok"] is True
    assert body["outcome"] == "rate_limited"


def test_parameter_rejection_still_counts_as_working(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that rejects the probe's shape has already accepted the key.
    The provider's message is kept: "the key works but the router model is
    wrong" is exactly what an operator needs to hear."""

    def reject(**_kw):
        raise _FakeBadRequest("unsupported parameter: max_output_tokens")

    _fake_openai(monkeypatch, reject)
    body = _test_key(client).json()
    assert body["ok"] is True
    assert body["outcome"] == "ok"
    assert "max_output_tokens" in body["detail"]


@pytest.mark.parametrize("exc", [_FakeTimeout, _FakeConnectionError])
def test_no_answer_is_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, exc: type[Exception]
) -> None:
    def hang(**_kw):
        raise exc("no route")

    _fake_openai(monkeypatch, hang)
    body = _test_key(client).json()
    assert body["ok"] is False
    assert body["outcome"] == "unreachable"


def test_any_other_failure_is_a_verdict_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(**_kw):
        raise RuntimeError("something unrelated")

    _fake_openai(monkeypatch, explode)
    res = _test_key(client)
    assert res.status_code == 200
    assert res.json() == {
        **res.json(),
        "ok": False,
        "outcome": "error",
    }


# --- the key never leaves the request ----------------------------------------------


def test_key_is_never_echoed_or_logged(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every outcome, including the logged catch-all, must keep the secret out
    of the response body and the log."""
    outcomes = [
        lambda **_kw: SimpleNamespace(output_text="pong"),
        _raise(_FakeAuthError),
        _raise(RuntimeError),
    ]
    for create in outcomes:
        _fake_openai(monkeypatch, create)
        with caplog.at_level(logging.DEBUG):
            res = _test_key(client)
        assert SECRET not in res.text
        assert SECRET not in caplog.text
        caplog.clear()


def _raise(exc: type[Exception]):
    def create(**_kw):
        raise exc("x")

    return create


def test_probes_the_router_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest model the app is configured with, and the one every
    auto-mode request needs anyway."""
    monkeypatch.setenv("OPENAI_MODEL_ROUTER", "gpt-5-nano-custom")
    used: dict = {}

    def create(**kw):
        used.update(kw)
        return SimpleNamespace(output_text="pong")

    _fake_openai(monkeypatch, create)
    body = _test_key(client).json()
    assert body["model"] == "gpt-5-nano-custom"
    assert used["model"] == "gpt-5-nano-custom"


def test_never_touches_the_cached_process_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_client() is a process-wide singleton bound to whatever
    OPENAI_API_KEY was at first use; routing a test through it would test
    the wrong key or poison the cache."""
    import app.orchestrator_calls as calls

    monkeypatch.setattr(
        calls, "get_client", lambda: pytest.fail("must construct a throwaway client")
    )
    _fake_openai(monkeypatch, lambda **_kw: SimpleNamespace(output_text="pong"))
    assert _test_key(client).json()["ok"] is True


def test_blank_key_is_rejected_by_validation(client: TestClient) -> None:
    assert _test_key(client, "").status_code == 422


# --- /v1/status ---------------------------------------------------------------------


def test_status_reports_credentials_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    assert client.get("/v1/status").json()["credentials_configured"] is True
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert client.get("/v1/status").json()["credentials_configured"] is False
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert client.get("/v1/status").json()["credentials_configured"] is False


def test_status_never_includes_the_key_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    assert SECRET not in client.get("/v1/status").text
