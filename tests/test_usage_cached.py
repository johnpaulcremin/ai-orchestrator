from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.orchestrator as orchestrator
from app.usage import Usage, estimate_cost


def test_cached_tokens_use_the_models_cached_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.delenv("CACHED_INPUT_MULTIPLIER", raising=False)
    # gpt-5: (1.25 input, 10.0 output, 0.125 cached). All input served from cache.
    cost = estimate_cost(
        "gpt-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
    )
    assert cost == pytest.approx(0.125)


def test_partial_cache_splits_input_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # Half of 1M input cached: 500k @ 1.25 + 500k @ 0.125 (per 1M).
    cost = estimate_cost(
        "gpt-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=500_000),
    )
    assert cost == pytest.approx(0.625 + 0.0625)


def test_no_cached_rate_uses_the_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.delenv("CACHED_INPUT_MULTIPLIER", raising=False)
    # claude-sonnet-5 has no 3rd price -> cached billed at input * 0.1 (default).
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
    )
    assert cost == pytest.approx(3.0 * 0.1)


def test_cached_multiplier_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.setenv("CACHED_INPUT_MULTIPLIER", "0.5")
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
    )
    assert cost == pytest.approx(3.0 * 0.5)


def test_zero_cached_is_identical_to_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # With no cached tokens the cost is exactly input + output pricing.
    cost = estimate_cost(
        "gpt-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(1.25 + 10.0)


def test_cached_cannot_exceed_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # Malformed data (cached > input) is clamped, never negative-billed.
    cost = estimate_cost(
        "gpt-5",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=9_000_000),
    )
    assert cost == pytest.approx(0.125)  # all 1M treated as cached, none double-charged


def test_model_pricing_env_accepts_a_cached_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PRICING", '{"custom-model": [4.0, 8.0, 1.0]}')
    hit = estimate_cost(
        "custom-model",
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
    )
    assert hit == pytest.approx(1.0)  # the explicit cached rate
    miss = estimate_cost("custom-model", Usage(input_tokens=1_000_000))
    assert miss == pytest.approx(4.0)  # no cache -> full input rate


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "not-a-number"])
def test_bad_cached_multiplier_falls_back_to_default(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.setenv("CACHED_INPUT_MULTIPLIER", bad)
    # A garbage multiplier must not produce NaN — it falls back to the 0.1 default.
    cost = estimate_cost(
        "claude-sonnet-5",  # 2-tuple, so the multiplier is used
        Usage(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=1_000_000),
    )
    assert cost == pytest.approx(3.0 * 0.1)


def test_zero_cached_stays_finite_under_bad_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CACHED_INPUT_MULTIPLIER", "nan")
    # The zero-cached byte-identical guarantee must survive a garbage multiplier.
    cost = estimate_cost(
        "claude-sonnet-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(3.0 + 15.0)


def test_non_finite_price_is_unpriced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICING", '{"weird": [1e999, 2.0]}')  # inf input rate
    assert estimate_cost("weird", Usage(input_tokens=1000, output_tokens=10)) is None


def test_record_openai_usage_captures_cached_tokens() -> None:
    usage = Usage()
    result = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            input_tokens_details=SimpleNamespace(cached_tokens=768),
        )
    )
    orchestrator._record_openai_usage(result, usage)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.cached_input_tokens == 768


def test_record_openai_usage_without_details_leaves_cached_zero() -> None:
    usage = Usage()
    result = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200)
    )
    orchestrator._record_openai_usage(result, usage)
    assert usage.cached_input_tokens == 0
