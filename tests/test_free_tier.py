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


# --- remaining_candidates_after() / exhaust_for_today() -------------------------


def test_remaining_candidates_after_returns_the_rest_of_the_ordered_list(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = "groq/llama-3.3-70b-versatile"
    third = "ollama/llama3.1:8b"
    monkeypatch.setenv("FREE_TIER_MODELS", f"{FREE_MODEL},{second},{third}")
    assert free_tier.remaining_candidates_after(FREE_MODEL) == [second, third]


def test_remaining_candidates_after_excludes_exhausted_candidates(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second = "groq/llama-3.3-70b-versatile"
    third = "ollama/llama3.1:8b"
    monkeypatch.setenv("FREE_TIER_MODELS", f"{FREE_MODEL},{second},{third}")
    monkeypatch.setenv(f"FREE_TIER_QUOTA_{free_tier._env_safe_name(second)}", "1")
    free_tier.record_use(second)
    assert free_tier.remaining_candidates_after(FREE_MODEL) == [third]


def test_remaining_candidates_after_empty_when_model_not_configured(
    db_path: Path, free_tier_configured: None
) -> None:
    assert free_tier.remaining_candidates_after("some-other-model") == []


def test_exhaust_for_today_makes_the_model_unavailable(
    db_path: Path, free_tier_configured: None
) -> None:
    assert free_tier.has_quota_remaining(FREE_MODEL) is True
    free_tier.exhaust_for_today(FREE_MODEL)
    assert free_tier.has_quota_remaining(FREE_MODEL) is False
    assert free_tier.pick_available_model() is None


def test_exhaust_for_today_only_affects_today(
    db_path: Path, free_tier_configured: None
) -> None:
    free_tier.exhaust_for_today(FREE_MODEL)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    # A prior day's usage is untouched by exhausting *today*.
    assert database.free_tier_usage_count(FREE_MODEL, yesterday) == 0


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
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_substitutes_for_fast_tier(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result.model == FREE_MODEL
    assert result.mode_used == f"auto->free:{FREE_MODEL}"
    assert free_tier.used_today(FREE_MODEL) == 1


def test_apply_free_tier_override_substitutes_for_budget_tier(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->budget")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result.model == FREE_MODEL


def test_apply_free_tier_override_never_touches_smart_tier_by_default(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->smart", model="primary-smart")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision
    assert free_tier.used_today(FREE_MODEL) == 0


def test_apply_free_tier_override_touches_smart_tier_when_free_lane_smart_enabled(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_LANE_SMART", "true")
    decision = _decision("auto->smart", model="primary-smart")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result.model == FREE_MODEL


def test_apply_free_tier_override_never_touches_a_forced_model(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("forced:claude-sonnet-5", model="claude-sonnet-5")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_never_touches_an_explicit_non_auto_mode(
    db_path: Path, free_tier_configured: None
) -> None:
    """mode_used == "fast" (an explicit, non-auto request) must never be
    substituted — only "auto->fast" (auto mode resolving to the fast tier)
    is eligible. See routing.decide_route: an explicit Mode.fast/budget/
    smart request gets mode_used == "fast"/"budget"/"smart" verbatim, with
    no "auto->" prefix."""
    decision = _decision("fast")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_never_touches_a_category_override(
    db_path: Path, free_tier_configured: None
) -> None:
    """mode_used containing ":" (e.g. "auto->fast:coding") means a
    per-category model override was configured for this request — the
    operator explicitly chose that model, so it must not be swapped out."""
    decision = _decision("auto->fast:coding")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_never_touches_a_tool_wanting_turn(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=True)
    assert result is decision
    assert free_tier.used_today(FREE_MODEL) == 0


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
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_noop_once_quota_exhausted(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "1")
    free_tier.record_use(FREE_MODEL)
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    assert result is decision


def test_apply_free_tier_override_noop_when_already_that_model(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast", model=FREE_MODEL)
    orchestrator._apply_free_tier_override(decision, tools_wanted=False)
    # No new usage recorded -- it wasn't a substitution, already resolved there.
    assert free_tier.used_today(FREE_MODEL) == 0


def test_apply_free_tier_override_preserves_the_tier_token_budget(
    db_path: Path, free_tier_configured: None
) -> None:
    decision = _decision("auto->fast")
    result = orchestrator._apply_free_tier_override(decision, tools_wanted=False)
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

    # "hi there" is a greeting -> the free prefilter shortcuts straight to
    # the fast tier with no classifier call, so auto mode is safe to use
    # here without stubbing a client response.
    result = orchestrator.run_orchestrator(
        AskRequest(question="hi there", mode=Mode.auto)
    )
    assert result.answer == "the answer"
    assert dispatched == [FREE_MODEL]
    assert result.cost_usd == 0.0
    assert result.mode_used == f"auto->free:{FREE_MODEL}"


def test_run_orchestrator_never_substitutes_an_explicit_fast_mode_request(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    dispatched: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: dispatched.append(str(kwargs["model"])) or "the answer",
    )

    orchestrator.run_orchestrator(AskRequest(question="hi there", mode=Mode.fast))
    assert dispatched == ["fast-model"]


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

    orchestrator.run_orchestrator(AskRequest(question="hi there", mode=Mode.auto))
    assert dispatched == ["fast-model"]


def test_run_orchestrator_logs_avoided_cost_for_a_free_tier_answer(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")

    def fake_call_model(**kwargs: object) -> str:
        fake_usage = kwargs.get("usage")
        if fake_usage is not None:
            fake_usage.input_tokens = 1000  # type: ignore[attr-defined]
            fake_usage.output_tokens = 1000  # type: ignore[attr-defined]
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    monkeypatch.setenv(
        "MODEL_PRICING", '{"fast-model": [1.0, 2.0]}'
    )  # a real price for the paid model this avoided

    orchestrator.run_orchestrator(AskRequest(question="hi there", mode=Mode.auto))

    assert database.avoided_cost_today(None) > 0


def test_run_orchestrator_falls_through_to_the_next_free_candidate_on_failure(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_free = "groq/llama-3.3-70b-versatile"
    monkeypatch.setenv("FREE_TIER_MODELS", f"{FREE_MODEL},{second_free}")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    attempted: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        model = str(kwargs["model"])
        attempted.append(model)
        if model == FREE_MODEL:
            raise RuntimeError("rate limited")
        fake_usage = kwargs.get("usage")
        if fake_usage is not None:
            fake_usage.input_tokens = 1  # type: ignore[attr-defined]
            fake_usage.output_tokens = 1  # type: ignore[attr-defined]
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hi there", mode=Mode.auto)
    )
    assert attempted == [FREE_MODEL, second_free]
    assert result.answer == "the answer"
    # The failed candidate is cooled down for the rest of the day.
    assert free_tier.has_quota_remaining(FREE_MODEL) is False


def test_run_orchestrator_falls_through_to_paid_once_all_free_candidates_fail(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model")
    attempted: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        model = str(kwargs["model"])
        attempted.append(model)
        if model == FREE_MODEL:
            raise RuntimeError("rate limited")
        return "the answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(
        AskRequest(question="hi there", mode=Mode.auto)
    )
    assert attempted == [FREE_MODEL, "fast-model"]
    assert result.answer == "the answer"


# --- Settings integration ------------------------------------------------------------


def test_free_tier_routing_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "FREE_TIER_ROUTING")
    assert flag["effective_enabled"] is True  # on by default
    assert flag["default"] is True


def test_free_lane_smart_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "FREE_LANE_SMART")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False


# --- Settings CRUD: FREE_TIER_MODELS / FREE_TIER_DEFAULT_QUOTA ------------------


def test_free_lane_section_appears_in_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    body = client.get("/v1/settings").json()
    keys = {entry["key"] for entry in body["free_lane"]}
    assert keys == {"FREE_TIER_MODELS", "FREE_TIER_DEFAULT_QUOTA"}
    models_entry = next(e for e in body["free_lane"] if e["key"] == "FREE_TIER_MODELS")
    assert models_entry["effective_value"] == ""
    assert models_entry["default"] == ""


def test_put_free_tier_models_saves_an_override(client: TestClient) -> None:
    res = client.put(
        "/v1/settings/FREE_TIER_MODELS",
        json={"value": f"{FREE_MODEL}, groq/llama-3.3-70b-versatile"},
    )
    assert res.status_code == 200
    entry = next(e for e in res.json()["free_lane"] if e["key"] == "FREE_TIER_MODELS")
    assert entry["effective_value"] == f"{FREE_MODEL},groq/llama-3.3-70b-versatile"
    assert entry["source"] == "override"


def test_put_free_tier_models_override_wins_over_env(
    client: TestClient, free_tier_configured: None
) -> None:
    other = "groq/llama-3.3-70b-versatile"
    res = client.put("/v1/settings/FREE_TIER_MODELS", json={"value": other})
    assert res.status_code == 200
    entry = next(e for e in res.json()["free_lane"] if e["key"] == "FREE_TIER_MODELS")
    assert entry["effective_value"] == other
    # And the resolution actually used by routing agrees.
    assert free_tier.configured_models() == [other]


def test_put_free_tier_models_rejects_an_invalid_model_name(
    client: TestClient,
) -> None:
    res = client.put(
        "/v1/settings/FREE_TIER_MODELS", json={"value": "not a valid model!"}
    )
    assert res.status_code == 400


def test_delete_free_tier_models_clears_the_override(client: TestClient) -> None:
    client.put("/v1/settings/FREE_TIER_MODELS", json={"value": FREE_MODEL})
    res = client.delete("/v1/settings/FREE_TIER_MODELS")
    assert res.status_code == 200
    entry = next(e for e in res.json()["free_lane"] if e["key"] == "FREE_TIER_MODELS")
    assert entry["override"] is None


def test_put_free_tier_default_quota_saves_an_override(client: TestClient) -> None:
    res = client.put("/v1/settings/FREE_TIER_DEFAULT_QUOTA", json={"value": "25"})
    assert res.status_code == 200
    entry = next(
        e for e in res.json()["free_lane"] if e["key"] == "FREE_TIER_DEFAULT_QUOTA"
    )
    assert entry["effective_value"] == "25"


def test_put_free_tier_default_quota_rejects_a_non_positive_value(
    client: TestClient,
) -> None:
    res = client.put("/v1/settings/FREE_TIER_DEFAULT_QUOTA", json={"value": "0"})
    assert res.status_code == 400
    res = client.put("/v1/settings/FREE_TIER_DEFAULT_QUOTA", json={"value": "abc"})
    assert res.status_code == 400


# --- GET /v1/free-tier -----------------------------------------------------------


def test_free_tier_status_endpoint_reports_quota_and_usage(
    db_path: Path, client: TestClient, free_tier_configured: None
) -> None:
    free_tier.record_use(FREE_MODEL)
    free_tier.record_use(FREE_MODEL)
    body = client.get("/v1/free-tier").json()
    assert body["enabled"] is True
    entry = next(m for m in body["models"] if m["model"] == FREE_MODEL)
    assert entry["quota"] == 100
    assert entry["used"] == 2
    assert entry["remaining"] == 98


def test_free_tier_status_endpoint_empty_when_disabled(
    db_path: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    body = client.get("/v1/free-tier").json()
    assert body["enabled"] is False
    assert body["models"] == []


def test_status_remaining_never_goes_negative(
    db_path: Path, free_tier_configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "1")
    free_tier.record_use(FREE_MODEL)
    free_tier.record_use(FREE_MODEL)  # over quota
    entry = free_tier.status()[0]
    assert entry["remaining"] == 0
