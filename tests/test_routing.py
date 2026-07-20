from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routing import _heuristic_route, _parse_classifier_json, decide_route
from app.schemas import Mode


class FakeClassifierClient:
    """Stands in for the OpenAI client during auto-routing tests."""

    def __init__(self, output_text: str) -> None:
        result = SimpleNamespace(output_text=output_text)
        self.responses = SimpleNamespace(create=lambda **kwargs: result)

    def with_options(self, **kwargs: object) -> FakeClassifierClient:
        return self


class RaisingClassifierClient:
    """Fake client whose classifier call always fails."""

    def __init__(self) -> None:
        def _raise(**kwargs: object) -> object:
            raise RuntimeError("classifier down")

        self.responses = SimpleNamespace(create=_raise)

    def with_options(self, **kwargs: object) -> RaisingClassifierClient:
        return self


class TestParseClassifierJson:
    def test_valid_json(self) -> None:
        raw = '{"category": "coding", "complexity": "high", "reason": "code task"}'
        assert _parse_classifier_json(raw) == {
            "category": "coding",
            "complexity": "high",
            "reason": "code task",
        }

    def test_code_fenced_json(self) -> None:
        raw = (
            "```json\n"
            '{"category": "math", "complexity": "low", "reason": "simple sum"}\n'
            "```"
        )
        assert _parse_classifier_json(raw) == {
            "category": "math",
            "complexity": "low",
            "reason": "simple sum",
        }

    def test_json_with_surrounding_prose(self) -> None:
        raw = (
            "Sure, here is my classification: "
            '{"category": "quick_fact", "complexity": "low", "reason": "lookup"} '
            "Hope that helps!"
        )
        parsed = _parse_classifier_json(raw)
        assert parsed is not None
        assert parsed["category"] == "quick_fact"
        assert parsed["complexity"] == "low"

    def test_invalid_category_returns_none(self) -> None:
        raw = '{"category": "juggling", "complexity": "low", "reason": "n/a"}'
        assert _parse_classifier_json(raw) is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_classifier_json("not json at all") is None
        assert _parse_classifier_json("") is None
        assert _parse_classifier_json("{broken json") is None

    def test_missing_complexity_defaults_to_medium(self) -> None:
        raw = '{"category": "coding"}'
        parsed = _parse_classifier_json(raw)
        assert parsed is not None
        assert parsed["complexity"] == "medium"
        assert parsed["reason"] == ""


class TestHeuristicRoute:
    def test_short_simple_question_routes_fast(self) -> None:
        decision = _heuristic_route("Hi, how are you?")
        assert decision.mode_used == "auto->fast"

    def test_marker_word_routes_smart(self) -> None:
        decision = _heuristic_route("Can you debug this for me?")
        assert decision.mode_used == "auto->smart"

    def test_long_question_routes_smart(self) -> None:
        question = "hello " * 40
        assert len(question) > 220
        decision = _heuristic_route(question)
        assert decision.mode_used == "auto->smart"


class TestDecideRouteExplicitModes:
    def test_fast_mode_uses_env_model_and_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
        monkeypatch.setenv("FAST_MAX_OUTPUT_TOKENS", "111")

        decision = decide_route("anything", Mode.fast)

        assert decision.model == "fast-model-x"
        assert decision.mode_used == "fast"
        assert decision.max_output_tokens == 111
        assert "Routed explicitly to FAST" in decision.notes

    def test_smart_mode_uses_env_model_and_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_SMART", "smart-model-y")
        monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "222")

        decision = decide_route("anything", Mode.smart)

        assert decision.model == "smart-model-y"
        assert decision.mode_used == "smart"
        assert decision.max_output_tokens == 222
        assert "Routed explicitly to SMART" in decision.notes


class TestReasoningEffort:
    def test_fast_defaults_to_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FAST_REASONING_EFFORT", raising=False)
        decision = decide_route("anything", Mode.fast)
        assert decision.reasoning_effort == "low"

    def test_smart_defaults_to_medium(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMART_REASONING_EFFORT", raising=False)
        decision = decide_route("anything", Mode.smart)
        assert decision.reasoning_effort == "medium"

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FAST_REASONING_EFFORT", "high")
        decision = decide_route("anything", Mode.fast)
        assert decision.reasoning_effort == "high"

    def test_invalid_env_value_falls_back_to_tier_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMART_REASONING_EFFORT", "turbo")
        decision = decide_route("anything", Mode.smart)
        assert decision.reasoning_effort == "medium"


class TestDecideRouteAuto:
    def test_smart_category_routes_smart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_MODEL_SMART", "smart-model-y")
        client = FakeClassifierClient(
            '{"category": "debugging", "complexity": "medium", "reason": "traceback"}'
        )

        decision = decide_route("Fix my stacktrace", Mode.auto, client=client)

        assert decision.mode_used == "auto->smart"
        assert decision.model == "smart-model-y"
        assert "AI router" in decision.notes
        assert "task=debugging" in decision.notes

    def test_fast_category_low_complexity_routes_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-model-x")
        client = FakeClassifierClient(
            '{"category": "quick_fact", "complexity": "low", "reason": "lookup"}'
        )

        decision = decide_route("Capital of France?", Mode.auto, client=client)

        assert decision.mode_used == "auto->fast"
        assert decision.model == "fast-model-x"
        assert "AI router" in decision.notes

    def test_no_client_falls_back_to_heuristic(self) -> None:
        decision = decide_route("Hi, how are you?", Mode.auto, client=None)
        assert decision.mode_used == "auto->fast"
        assert "Heuristic fallback" in decision.notes

    def test_classifier_failure_falls_back_to_heuristic(self) -> None:
        decision = decide_route(
            "Please debug my code",
            Mode.auto,
            client=RaisingClassifierClient(),
        )
        assert decision.mode_used == "auto->smart"
        assert "Heuristic fallback" in decision.notes

    def test_category_model_override_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_SMART", "smart-default")
        monkeypatch.setenv("SMART_MAX_OUTPUT_TOKENS", "4000")
        monkeypatch.setenv("MODEL_CODING", "claude-sonnet-5")
        client = FakeClassifierClient(
            '{"category": "coding", "complexity": "medium", "reason": "code"}'
        )

        decision = decide_route("write a function", Mode.auto, client=client)

        # Category override picks the model; smart tier still sets the budget.
        assert decision.model == "claude-sonnet-5"
        assert decision.mode_used == "auto->smart:coding"
        assert decision.max_output_tokens == 4000
        assert "category model claude-sonnet-5" in decision.notes

    def test_category_model_override_on_fast_category(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_FAST", "fast-default")
        monkeypatch.setenv("MODEL_QUICK_FACT", "groq/llama-3.3-70b-versatile")
        client = FakeClassifierClient(
            '{"category": "quick_fact", "complexity": "low", "reason": "lookup"}'
        )

        decision = decide_route("2+2?", Mode.auto, client=client)

        assert decision.model == "groq/llama-3.3-70b-versatile"
        assert decision.mode_used == "auto->fast:quick_fact"

    def test_no_category_override_uses_tier_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_MODEL_SMART", "smart-default")
        monkeypatch.delenv("MODEL_REASONING", raising=False)
        client = FakeClassifierClient(
            '{"category": "reasoning", "complexity": "high", "reason": "logic"}'
        )

        decision = decide_route("explain the tradeoffs", Mode.auto, client=client)

        assert decision.model == "smart-default"
        assert decision.mode_used == "auto->smart"

    def test_decision_exposes_predicted_category(self) -> None:
        client = FakeClassifierClient(
            '{"category": "coding", "complexity": "medium", "reason": "code"}'
        )
        decision = decide_route("write a function", Mode.auto, client=client)
        # The classifier's category is exposed for the eval harness, even with
        # no per-category override configured (so mode_used stays "auto->smart").
        assert decision.category == "coding"
        assert decision.mode_used == "auto->smart"

    def test_explicit_mode_has_no_predicted_category(self) -> None:
        assert decide_route("anything", Mode.fast).category == ""
        assert decide_route("anything", Mode.smart).category == ""

    def test_heuristic_fallback_has_no_predicted_category(self) -> None:
        assert decide_route("Hi there", Mode.auto, client=None).category == ""
