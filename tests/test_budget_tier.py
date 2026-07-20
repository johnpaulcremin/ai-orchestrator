"""BUDGET tier: an opt-in tier below FAST for bulk / low-stakes work.

Enabled only when OPENAI_MODEL_BUDGET is set. In auto mode a low-complexity
fast-category task (and a pure-greeting prefilter hit) drops to the budget model
with a tight token budget + minimal reasoning; medium-complexity fast tasks stay
on FAST and smart tasks stay on SMART. Also selectable explicitly (mode=budget)
and as a conversation pin. Composes with the daily spend cap and cost log.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cache
from app.main import _pinned_ask_request
from app.routing import decide_route
from app.schemas import AskRequest, Mode


def _classifier(category: str, complexity: str) -> object:
    """A fake OpenAI client whose classifier returns this category/complexity."""
    text = f'{{"category": "{category}", "complexity": "{complexity}", "reason": "t"}}'
    result = SimpleNamespace(output_text=text)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kw: result))
    client.with_options = lambda **_kw: client  # type: ignore[attr-defined]
    return client


# --- auto routing ------------------------------------------------------------


def test_auto_low_complexity_fast_category_routes_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    monkeypatch.setenv("BUDGET_MAX_OUTPUT_TOKENS", "800")
    d = decide_route(
        "Capital of France?", Mode.auto, client=_classifier("quick_fact", "low")
    )
    assert d.mode_used == "auto->budget"
    assert d.model == "budget-model-x"
    assert d.max_output_tokens == 800
    assert d.reasoning_effort == "minimal"


def test_auto_medium_complexity_fast_category_stays_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
    d = decide_route(
        "Summarize this report",
        Mode.auto,
        client=_classifier("summarization", "medium"),
    )
    assert d.mode_used == "auto->fast"
    assert d.model == "fast-model-x"


def test_auto_low_complexity_without_budget_model_stays_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_MODEL_BUDGET", raising=False)  # tier disabled
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
    d = decide_route(
        "Capital of France?", Mode.auto, client=_classifier("quick_fact", "low")
    )
    assert d.mode_used == "auto->fast"
    assert d.model == "fast-model-x"


def test_auto_smart_category_ignores_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "smart-model-y")
    # A smart category stays smart even at low complexity.
    d = decide_route("write a function", Mode.auto, client=_classifier("coding", "low"))
    assert d.mode_used == "auto->smart"
    assert d.model == "smart-model-y"


def test_category_override_wins_on_budget_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    monkeypatch.setenv("MODEL_QUICK_FACT", "groq/llama-3.3-70b-versatile")
    monkeypatch.setenv("BUDGET_MAX_OUTPUT_TOKENS", "800")
    d = decide_route("2+2?", Mode.auto, client=_classifier("quick_fact", "low"))
    # The category override picks the model; the budget tier still sets the budget.
    assert d.model == "groq/llama-3.3-70b-versatile"
    assert d.mode_used == "auto->budget:quick_fact"
    assert d.max_output_tokens == 800


# --- prefilter ---------------------------------------------------------------


def test_prefilter_greeting_routes_budget_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    # The classifier client is present but the greeting prefilter fires first.
    d = decide_route("hi there", Mode.auto, client=_classifier("coding", "high"))
    assert d.mode_used == "auto->budget"
    assert d.model == "budget-model-x"


def test_prefilter_greeting_routes_fast_without_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_MODEL_BUDGET", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
    d = decide_route("hi there", Mode.auto, client=_classifier("coding", "high"))
    assert d.mode_used == "auto->fast"


# --- explicit mode -----------------------------------------------------------


def test_explicit_budget_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    monkeypatch.setenv("BUDGET_MAX_OUTPUT_TOKENS", "500")
    monkeypatch.setenv("BUDGET_REASONING_EFFORT", "low")
    d = decide_route("anything", Mode.budget)
    assert d.mode_used == "budget"
    assert d.model == "budget-model-x"
    assert d.max_output_tokens == 500
    assert d.reasoning_effort == "low"


def test_explicit_budget_mode_falls_back_to_fast_model_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_MODEL_BUDGET", raising=False)
    monkeypatch.delenv("BUDGET_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
    d = decide_route("anything", Mode.budget)
    assert d.model == "fast-model-x"  # no budget model -> reuse fast's model
    assert d.max_output_tokens == 800  # ...but still the tight budget default


def test_budget_is_a_valid_request_mode() -> None:
    assert AskRequest(question="x", mode="budget").mode == Mode.budget


def test_budget_pin_selects_the_budget_tier() -> None:
    pinned = _pinned_ask_request(
        {"pinned_model": "budget"}, "new question", AskRequest(question="orig")
    )
    assert pinned.mode == Mode.budget
    assert pinned.model is None  # a tier pin, not a forced exact model


# --- cache invalidation ------------------------------------------------------


def test_config_signature_changes_with_budget_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_MODEL_BUDGET", raising=False)
    before = cache.make_key("q", "auto")
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-model-x")
    after = cache.make_key("q", "auto")
    assert before != after  # changing the budget model invalidates cached entries
