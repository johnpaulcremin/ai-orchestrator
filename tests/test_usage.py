from __future__ import annotations

import pytest

from app.usage import (
    Usage,
    estimate_cost,
    estimate_speech_cost,
    estimate_transcription_cost,
)


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


def test_estimate_cost_ollama_is_zero_not_unpriced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local inference is genuinely free: 0.0 (shown as $0, bounded by the
    # budget gate), NOT None ("unpriced", which the gate can't bound).
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    cost = estimate_cost(
        "ollama/llama3.1:8b", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == 0.0


def test_estimate_cost_model_pricing_override_beats_ollama_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anyone deliberately pricing local compute via MODEL_PRICING still wins.
    monkeypatch.setenv("MODEL_PRICING", '{"ollama/llama3.1:8b": [1.0, 2.0]}')
    cost = estimate_cost(
        "ollama/llama3.1:8b", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(3.0)


def test_estimate_cost_bare_name_pricing_also_beats_ollama_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The $0 short-circuit must sit AFTER the bare-name table fallback, not
    # between the two lookups — a bare-name MODEL_PRICING entry still wins.
    monkeypatch.setenv("MODEL_PRICING", '{"llama3.1:8b": [1.0, 2.0]}')
    cost = estimate_cost(
        "ollama/llama3.1:8b", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(3.0)


def test_estimate_cost_ollama_cloud_tags_stay_unpriced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "*-cloud" tags are proxied by the local daemon to Ollama's usage-metered
    # PAID cloud — they must NOT be silently booked as free.
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    assert (
        estimate_cost(
            "ollama/gpt-oss:120b-cloud", Usage(input_tokens=100, output_tokens=100)
        )
        is None
    )


def test_estimate_cost_ollama_chat_prefix_is_also_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LiteLLM's "ollama_chat/" prefix hits the same local server.
    monkeypatch.delenv("MODEL_PRICING", raising=False)
    cost = estimate_cost(
        "ollama_chat/llama3.1:8b", Usage(input_tokens=100, output_tokens=100)
    )
    assert cost == 0.0


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


# --- estimate_speech_cost / estimate_transcription_cost -----------------------


def test_estimate_speech_cost_scales_with_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEECH_COST_PER_1K_CHARS_USD", raising=False)
    assert estimate_speech_cost("x" * 2000) == pytest.approx(2 * 0.015)
    assert estimate_speech_cost("") == 0.0


def test_estimate_speech_cost_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECH_COST_PER_1K_CHARS_USD", "1.0")
    assert estimate_speech_cost("x" * 1000) == pytest.approx(1.0)


def test_estimate_speech_cost_bad_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEECH_COST_PER_1K_CHARS_USD", "not-a-number")
    assert estimate_speech_cost("x" * 1000) == pytest.approx(0.015)


def test_estimate_transcription_cost_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIPTION_COST_PER_CALL_USD", raising=False)
    assert estimate_transcription_cost() == pytest.approx(0.006)


def test_estimate_transcription_cost_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIPTION_COST_PER_CALL_USD", "0.5")
    assert estimate_transcription_cost() == pytest.approx(0.5)


def test_estimate_transcription_cost_negative_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIPTION_COST_PER_CALL_USD", "-1")
    assert estimate_transcription_cost() == pytest.approx(0.006)
