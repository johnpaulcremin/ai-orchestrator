"""Cache-WRITE token pricing (Usage.cache_write_input_tokens) — the surcharge
side of provider prompt caching, distinct from test_usage_cached.py's
cache-READ (discount) tokens. Anthropic's cache_creation_input_tokens is
billed at a PREMIUM over normal input, the opposite direction from a cache
read, since establishing a cache entry costs the provider extra work.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.providers import _record_anthropic
from app.usage import Usage, estimate_cost


def test_cache_write_tokens_use_the_models_cache_write_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.delenv("CACHE_WRITE_MULTIPLIER", raising=False)
    monkeypatch.setenv("MODEL_PRICING", '{"custom-model": [4.0, 8.0, 1.0, 5.0]}')
    cost = estimate_cost(
        "custom-model",
        Usage(
            input_tokens=1_000_000, output_tokens=0, cache_write_input_tokens=1_000_000
        ),
    )
    assert cost == pytest.approx(5.0)  # the explicit cache-write rate


def test_no_cache_write_rate_uses_the_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.delenv("CACHE_WRITE_MULTIPLIER", raising=False)
    # claude-sonnet-5 has no 4th price -> cache-write billed at input * 1.25 (default).
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(
            input_tokens=1_000_000, output_tokens=0, cache_write_input_tokens=1_000_000
        ),
    )
    assert cost == pytest.approx(2.0 * 1.25)


def test_cache_write_multiplier_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.setenv("CACHE_WRITE_MULTIPLIER", "2.0")
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(
            input_tokens=1_000_000, output_tokens=0, cache_write_input_tokens=1_000_000
        ),
    )
    assert cost == pytest.approx(2.0 * 2.0)


def test_cache_write_multiplier_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # A premium must never be cheaper than normal input, even with a bad env value.
    monkeypatch.setenv("CACHE_WRITE_MULTIPLIER", "0.01")
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(
            input_tokens=1_000_000, output_tokens=0, cache_write_input_tokens=1_000_000
        ),
    )
    assert cost == pytest.approx(2.0 * 1.0)


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "not-a-number"])
def test_bad_cache_write_multiplier_falls_back_to_default(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.setenv("CACHE_WRITE_MULTIPLIER", bad)
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(
            input_tokens=1_000_000, output_tokens=0, cache_write_input_tokens=1_000_000
        ),
    )
    assert cost == pytest.approx(2.0 * 1.25)


def test_cache_write_and_cache_read_combine_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    monkeypatch.delenv("CACHED_INPUT_MULTIPLIER", raising=False)
    monkeypatch.delenv("CACHE_WRITE_MULTIPLIER", raising=False)
    # claude-sonnet-5: (2.0 input, 10.0 output). 500k read (@0.1x), 300k write
    # (@1.25x), 200k plain input — 1M total.
    cost = estimate_cost(
        "claude-sonnet-5",
        Usage(
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=500_000,
            cache_write_input_tokens=300_000,
        ),
    )
    expected = (
        0.2 * 2.0  # 200k plain @ input rate
        + 0.5 * (2.0 * 0.1)  # 500k cache-read @ discount
        + 0.3 * (2.0 * 1.25)  # 300k cache-write @ premium
    )
    assert cost == pytest.approx(expected)


def test_cache_write_plus_cached_cannot_exceed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    # Malformed data (cached + cache_write > input) is clamped, never
    # negative-billed or double-counted — cached wins the overlap.
    cost = estimate_cost(
        "gpt-5",  # (1.25 input, 10.0 output, 0.125 cached)
        Usage(
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=800_000,
            cache_write_input_tokens=800_000,
        ),
    )
    expected = 0.8 * 0.125 + 0.2 * (1.25 * 1.25)  # 800k cached, 200k cache-write
    assert cost == pytest.approx(expected)


def test_zero_cache_write_is_identical_to_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    cost = estimate_cost(
        "gpt-5", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(1.25 + 10.0)


# --- providers._record_anthropic ----------------------------------------------


def test_record_anthropic_folds_cache_read_and_write_into_input_tokens() -> None:
    usage = Usage()
    source = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=2000,
        cache_creation_input_tokens=300,
    )
    _record_anthropic(usage, source)

    assert usage.input_tokens == 100 + 2000 + 300
    assert usage.output_tokens == 50
    assert usage.cached_input_tokens == 2000
    assert usage.cache_write_input_tokens == 300


def test_record_anthropic_without_cache_fields_leaves_them_zero() -> None:
    usage = Usage()
    source = SimpleNamespace(input_tokens=100, output_tokens=50)
    _record_anthropic(usage, source)

    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cached_input_tokens == 0
    assert usage.cache_write_input_tokens == 0


def test_record_anthropic_none_source_is_a_noop() -> None:
    usage = Usage(input_tokens=5, output_tokens=5)
    _record_anthropic(usage, None)
    assert usage.input_tokens == 5
    assert usage.output_tokens == 5


def test_record_anthropic_none_usage_is_a_noop() -> None:
    # Must not raise when usage tracking wasn't requested.
    _record_anthropic(None, SimpleNamespace(input_tokens=1, output_tokens=1))
