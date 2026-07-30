"""Generic local OpenAI-compatible inference servers (app/local_endpoints.py;
LOCAL_ENDPOINTS + the "local:<name>/<model>" scheme) — parity with the
existing local-Ollama treatment (pricing/budget/auth-messaging), extended to
any named local server rather than one hardcoded provider.
"""

from __future__ import annotations

import pytest

from app import local_endpoints
from app.budget import reserve
from app.providers import _litellm_kwargs, key_env_for, provider_of
from app.usage import Usage, estimate_cost

# --- LOCAL_ENDPOINTS parsing ---------------------------------------------------


def test_endpoints_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_ENDPOINTS", raising=False)
    assert local_endpoints.endpoints() == {}


def test_endpoints_parses_json_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LOCAL_ENDPOINTS",
        '{"lmstudio": "http://localhost:1234/v1", "vllm": "http://localhost:8000/v1"}',
    )
    assert local_endpoints.endpoints() == {
        "lmstudio": "http://localhost:1234/v1",
        "vllm": "http://localhost:8000/v1",
    }


def test_endpoints_malformed_json_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", "not-json{")
    assert local_endpoints.endpoints() == {}


def test_endpoints_non_object_json_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '["not", "an", "object"]')
    assert local_endpoints.endpoints() == {}


def test_endpoints_drops_entries_with_empty_name_or_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LOCAL_ENDPOINTS", '{"": "http://x", "good": "http://y", "bad": ""}'
    )
    assert local_endpoints.endpoints() == {"good": "http://y"}


# --- is_local_endpoint_model() / parse() ----------------------------------------


def test_is_local_endpoint_model() -> None:
    assert local_endpoints.is_local_endpoint_model("local:lmstudio/llama-3.1") is True
    assert local_endpoints.is_local_endpoint_model("gpt-5") is False
    assert local_endpoints.is_local_endpoint_model("ollama/llama3.1:8b") is False


def test_parse_splits_name_and_model() -> None:
    assert local_endpoints.parse("local:lmstudio/llama-3.1-8b-instruct") == (
        "lmstudio",
        "llama-3.1-8b-instruct",
    )


def test_parse_handles_a_model_id_containing_slashes() -> None:
    # split(..., 1) means only the FIRST "/" separates name from model id —
    # the model id itself may contain more slashes (e.g. a HF-style path).
    assert local_endpoints.parse("local:vllm/org/model-name") == (
        "vllm",
        "org/model-name",
    )


def test_parse_returns_none_for_non_local_models() -> None:
    assert local_endpoints.parse("gpt-5") is None
    assert local_endpoints.parse("ollama/llama3.1:8b") is None


def test_parse_returns_none_when_malformed() -> None:
    assert local_endpoints.parse("local:noslash") is None
    assert local_endpoints.parse("local:/model") is None
    assert local_endpoints.parse("local:name/") is None


def test_base_url_for_returns_the_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    assert (
        local_endpoints.base_url_for("local:lmstudio/llama-3.1")
        == "http://localhost:1234/v1"
    )


def test_base_url_for_none_when_name_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    assert local_endpoints.base_url_for("local:unknown/llama-3.1") is None


def test_base_url_for_none_for_a_non_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    assert local_endpoints.base_url_for("gpt-5") is None


# --- providers._litellm_kwargs: dispatch translation ----------------------------


def test_litellm_kwargs_translates_a_configured_local_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    kwargs = _litellm_kwargs(
        "local:lmstudio/llama-3.1-8b-instruct", "hi", 100, 30.0, ""
    )
    assert kwargs["model"] == "openai/llama-3.1-8b-instruct"
    assert kwargs["api_base"] == "http://localhost:1234/v1"
    assert kwargs["api_key"]  # some non-empty placeholder, never a real secret


def test_litellm_kwargs_leaves_an_unconfigured_local_model_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_ENDPOINTS", raising=False)
    kwargs = _litellm_kwargs("local:unknown/llama-3.1", "hi", 100, 30.0, "")
    assert kwargs["model"] == "local:unknown/llama-3.1"
    assert "api_base" not in kwargs


def test_litellm_kwargs_unaffected_for_an_ordinary_litellm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    kwargs = _litellm_kwargs("gemini/gemini-flash-latest", "hi", 100, 30.0, "")
    assert kwargs["model"] == "gemini/gemini-flash-latest"
    assert "api_base" not in kwargs


# --- provider_of / key_env_for parity with Ollama --------------------------------


def test_provider_of_routes_local_models_through_litellm() -> None:
    assert provider_of("local:lmstudio/llama-3.1") == "litellm"


def test_key_env_for_local_model_names_local_endpoints_not_a_fake_env_var() -> None:
    message = key_env_for("local:lmstudio/llama-3.1")
    assert "LOCAL_ENDPOINTS" in message
    assert "running" in message


# --- usage.estimate_cost: $0, parity with Ollama --------------------------------


def test_estimate_cost_local_model_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    cost = estimate_cost(
        "local:lmstudio/llama-3.1-8b",
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert cost == 0.0


def test_estimate_cost_model_pricing_override_beats_local_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PRICING", '{"local:lmstudio/llama-3.1-8b": [1.0, 2.0]}')
    cost = estimate_cost(
        "local:lmstudio/llama-3.1-8b",
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert cost == pytest.approx(3.0)


# --- budget.reserve: never blocked, parity with Ollama --------------------------


def test_reserve_never_blocks_a_local_endpoint_model(
    db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.01")
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    note, reservation_id = reserve("local:lmstudio/llama-3.1-8b", 100_000)
    assert note is None
    assert reservation_id is None  # a $0 call reserves nothing to reconcile
