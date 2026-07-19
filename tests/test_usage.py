from __future__ import annotations

import pytest

from app.usage import Usage, estimate_cost


def test_usage_total_tokens() -> None:
    assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15


def test_estimate_cost_for_a_known_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # gpt-5-mini is $0.25 / $2.00 per 1M tokens.
    cost = estimate_cost(
        "gpt-5-mini", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(0.25 + 2.0)


def test_estimate_cost_falls_back_to_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # "gemini/gemini-flash-lite-latest" is priced; the bare-name path also works.
    cost = estimate_cost(
        "gemini/gemini-flash-lite-latest",
        Usage(input_tokens=1_000_000, output_tokens=0),
    )
    assert cost == pytest.approx(0.10)


def test_estimate_cost_unknown_model_is_none() -> None:
    assert (
        estimate_cost(
            "totally-unknown-model", Usage(input_tokens=100, output_tokens=100)
        )
        is None
    )


def test_estimate_cost_none_usage_is_none() -> None:
    assert estimate_cost("gpt-5", None) is None


def test_model_pricing_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICING", '{"custom-model": [4.0, 8.0]}')
    cost = estimate_cost(
        "custom-model", Usage(input_tokens=1_000_000, output_tokens=500_000)
    )
    assert cost == pytest.approx(4.0 + 4.0)


def test_model_pricing_bad_json_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICING", "not-json{")
    # Falls back to the default table without raising.
    assert estimate_cost(
        "gpt-5", Usage(input_tokens=1_000_000, output_tokens=0)
    ) == pytest.approx(1.25)
