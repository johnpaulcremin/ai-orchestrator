"""Free-first routing (app/free_tier.py): user-configured free-tier models
tried before the paid budget/fast tier, with a self-tracked daily request
quota — see the module docstring for why this is configured, not hardcoded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, free_tier, orchestrator, usage
from app.routing import RouteDecision
from app.schemas import AskRequest, Mode

FREE_MODEL = "gemini/gemini-flash-latest"


@pytest.fixture()
def free_tier_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREE_TIER_MODELS", FREE_MODEL)


# --- config parsing ----------------------------------------------------------------


def test_configured_models_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    assert free_tier.configured_models() == []


def test_configured_models_parses_a_comma_separated_ordered_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FREE_TIER_MODELS", "gemini/gemini-flash-latest, groq/llama-3.3-70b-versatile"
    )
    assert free_tier.configured_models() == [
        "gemini/gemini-flash-latest",
        "groq/llama-3.3-70b-versatile",
    ]


def test_enabled_false_when_no_models_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    assert free_tier.enabled() is False


def test_enabled_true_when_configured_and_flag_on(
    free_tier_configured: None,
) -> None:
    assert free_tier.enabled() is True


def test_enabled_false_when_flag_explicitly_off(
    free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_ROUTING", "false")
    assert free_tier.enabled() is False


def test_is_free_tier_model(free_tier_configured: None) -> None:
    assert free_tier.is_free_tier_model(FREE_MODEL) is True
    assert free_tier.is_free_tier_model("gpt-5") is False


def test_daily_quota_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "FREE_TIER_QUOTA_" + free_tier._env_safe_name(FREE_MODEL), raising=False
    )
    monkeypatch.delenv("FREE_TIER_DEFAULT_QUOTA", raising=False)
    assert free_tier.daily_quota(FREE_MODEL) == 100


def test_daily_quota_default_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "50")
    assert free_tier.daily_quota(FREE_MODEL) == 50


def test_daily_quota_per_model_override_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "50")
    monkeypatch.setenv(f"FREE_TIER_QUOTA_{free_tier._env_safe_name(FREE_MODEL)}", "5")
    assert free_tier.daily_quota(FREE_MODEL) == 5


def test_daily_quota_invalid_or_non_positive_falls_back_to_builtin_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "not-a-number")
    assert free_tier.daily_quota(FREE_MODEL) == 100
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "0")
    assert free_tier.daily_quota(FREE_MODEL) == 100


def test_env_safe_name_normalizes_model_strings() -> None:
    assert (
        free_tier._env_safe_name("gemini/gemini-flash-latest")
        == "GEMINI_GEMINI_FLASH_LATEST"
    )
    assert (
        free_tier._env_safe_name("openrouter/meta-llama/llama-3.1:free")
        == "OPENROUTER_META_LLAMA_LLAMA_3_1_FREE"
    )


# --- usage tracking / quota -----------------------------------------------------


def test_used_today_starts_at_zero(db_path: Path) -> None:
    assert free_tier.used_today(FREE_MODEL) == 0


def test_record_use_increments_used_today(db_path: Path) -> None:
    free_tier.record_use(FREE_MODEL)
    free_tier.record_use(FREE_MODEL)
    assert free_tier.used_today(FREE_MODEL) == 2


def test_record_use_is_scoped_per_model(db_path: Path) -> None:
    free_tier.record_use(FREE_MODEL)
    assert free_tier.used_today("groq/llama-3.3-70b-versatile") == 0


def test_has_quota_remaining_true_under_the_cap(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "2")
    free_tier.record_use(FREE_MODEL)
    assert free_tier.has_quota_remaining(FREE_MODEL) is True


def test_has_quota_remaining_false_once_exhausted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "2")
    free_tier.record_use(FREE_MODEL)
    free_tier.record_use(FREE_MODEL)
    assert free_tier.has_quota_remaining(FREE_MODEL) is False


def test_usage_does_not_carry_over_from_a_previous_day(db_path: Path) -> None:
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    database.free_tier_usage_increment(FREE_MODEL, yesterday)
    assert free_tier.used_today(FREE_MODEL) == 0


# --- pick_available_model() ----------------------------------------------------


def test_pick_available_model_returns_none_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    assert free_tier.pick_available_model() is None


def test_pick_available_model_returns_the_first_configured_model(
    db_path: Path, free_tier_configured: None
) -> None:
    assert free_tier.pick_available_model() == FREE_MODEL


def test_pick_available_model_skips_an_exhausted_model_for_the_next_one(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = "groq/llama-3.3-70b-versatile"
    monkeypatch.setenv("FREE_TIER_MODELS", f"{FREE_MODEL},{second}")
    monkeypatch.setenv(f"FREE_TIER_QUOTA_{free_tier._env_safe_name(FREE_MODEL)}", "1")
    free_tier.record_use(FREE_MODEL)
    assert free_tier.pick_available_model() == second


def test_pick_available_model_returns_none_when_all_exhausted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_MODELS", FREE_MODEL)
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "1")
    free_tier.record_use(FREE_MODEL)
    assert free_tier.pick_available_model() is None


# --- usage.estimate_cost integration: $0 for a free-tier model -----------------


def test_estimate_cost_is_zero_for_a_configured_free_tier_model(
    free_tier_configured: None,
) -> None:
    assert (
        usage.estimate_cost(
            FREE_MODEL, usage.Usage(input_tokens=1000, output_tokens=1000)
        )
        == 0.0
    )


def test_estimate_cost_unaffected_for_a_model_not_listed_as_free_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    # An entirely unknown model stays unpriced (None), same as before this feature.
    assert (
        usage.estimate_cost(
            "totally-unknown-model-xyz", usage.Usage(input_tokens=1, output_tokens=1)
        )
        is None
    )


# --- orchestrator integration: _apply_free_tier_override ------------------------


def _decision(mode_used: str, model: str = "fallback-fast") -> RouteDecision:
    return RouteDecision(
        model=model,
        mode_used=mode_used,
        notes="n",
        max_output_tokens=800,
        reasoning_effort="minimal",
    )


def test_apply_free_tier_override_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision)
    assert result is decision


def test_apply_free_tier_override_substitutes_for_fast_tier(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision)
    assert result.model == FREE_MODEL
    assert "free_tier" in result.mode_used
    assert free_tier.used_today(FREE_MODEL) == 1


def test_apply_free_tier_override_substitutes_for_budget_tier(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->budget")
    result = orchestrator._apply_free_tier_override(decision)
    assert result.model == FREE_MODEL


def test_apply_free_tier_override_never_touches_smart_tier(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->smart", model="primary-smart")
    result = orchestrator._apply_free_tier_override(decision)
    assert result is decision
    assert free_tier.used_today(FREE_MODEL) == 0


def test_apply_free_tier_override_never_touches_a_forced_model(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("forced:claude-sonnet-5", model="claude-sonnet-5")
    result = orchestrator._apply_free_tier_override(decision)
    assert result is decision


def test_apply_free_tier_override_never_touches_an_ambiguous_decision(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = RouteDecision(
        model="fallback-fast",
        mode_used="auto->clarify",
        notes="n",
        max_output_tokens=0,
        reasoning_effort="minimal",
        ambiguous=True,
        clarifying_question="Which one do you mean?",
    )
    result = orchestrator._apply_free_tier_override(decision)
    assert result is decision


def test_apply_free_tier_override_noop_once_quota_exhausted(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "1")
    free_tier.record_use(FREE_MODEL)
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision)
    assert result is decision


def test_apply_free_tier_override_noop_when_already_that_model(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast", model=FREE_MODEL)
    orchestrator._apply_free_tier_override(decision)
    # No new usage recorded -- it wasn't a substitution, already resolved there.
    assert free_tier.used_today(FREE_MODEL) == 0


def test_apply_free_tier_override_preserves_the_tier_token_budget(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision)
    assert result.max_output_tokens == decision.max_output_tokens
    assert result.reasoning_effort == decision.reasoning_effort


# --- end-to-end: run_orchestrator actually dispatches to the free-tier model ----


def test_run_orchestrator_dispatches_to_the_free_tier_model(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    dispatched: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        dispatched.append(str(kwargs["model"]))
        fake_usage = kwargs.get("usage")
        if fake_usage is not None:
            fake_usage.input_tokens = 10  # type: ignore[attr-defined]
            fake_usage.output_tokens = 5  # type: ignore[attr-defined]
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hi there", mode=Mode.fast)
    )
    assert result.answer == "the answer"
    assert dispatched == [FREE_MODEL]
    assert result.cost_usd == 0.0


def test_run_orchestrator_falls_back_to_paid_tier_once_quota_exhausted(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "1")
    free_tier.record_use(FREE_MODEL)
    dispatched: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        dispatched.append(str(kwargs["model"]))
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="hi there", mode=Mode.fast))
    assert dispatched == ["fast-model"]


# --- Settings integration ------------------------------------------------------------


def test_free_tier_routing_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "FREE_TIER_ROUTING")
    assert flag["effective_enabled"] is True  # on by default
    assert flag["default"] is True
