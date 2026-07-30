"""Per-category role prompts (CATEGORY_PROMPT_<CATEGORY>): an optional
system-prompt persona folded into the outgoing prompt whenever auto-mode
routing resolves a task category — e.g. a coder persona for `coding`, a
writer persona for `creative_writing`. Off by default (every category starts
with an empty prompt), same override > env > default resolution chain as
MODEL_<CATEGORY> (app/settings.py's model_setting), and same 4,000-char cap
as a per-conversation custom-instructions field.

Deliberately lives inside the STABLE, cacheable prompt prefix (prepended,
never appended) so it can only ever grow what Anthropic's cache_control
checkpointing / OpenAI's implicit prefix caching already treat as stable —
see app/orchestrator.py's apply_category_role_prompt docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app.orchestrator import apply_category_role_prompt
from app.schemas import AskRequest, Mode
from app.settings import (
    PROMPT_KEYS,
    SETTABLE_KEYS,
    category_prompt_key,
    describe_settings,
    validate_prompt_value,
)

# --- settings.py: keys + validation ---------------------------------------------


def test_prompt_keys_cover_every_category() -> None:
    assert category_prompt_key("coding") == "CATEGORY_PROMPT_CODING"
    assert len(PROMPT_KEYS) == 11
    assert "CATEGORY_PROMPT_CODING" in PROMPT_KEYS
    assert "CATEGORY_PROMPT_CREATIVE_WRITING" in PROMPT_KEYS


def test_prompt_keys_are_settable() -> None:
    assert frozenset(PROMPT_KEYS) <= SETTABLE_KEYS


def test_validate_prompt_value_accepts_and_strips_free_text() -> None:
    assert validate_prompt_value("  You are a senior Python engineer.  ") == (
        "You are a senior Python engineer."
    )


def test_validate_prompt_value_empty_is_valid_clear() -> None:
    assert validate_prompt_value("") == ""
    assert validate_prompt_value("   ") == ""


def test_validate_prompt_value_rejects_oversized() -> None:
    with pytest.raises(ValueError, match="too long"):
        validate_prompt_value("x" * 4_001)


def test_validate_prompt_value_accepts_at_the_cap() -> None:
    assert validate_prompt_value("x" * 4_000) == "x" * 4_000


def test_validate_prompt_value_allows_characters_a_model_name_would_reject() -> None:
    # Unlike validate_model_value, free prose (punctuation, newlines) is fine.
    text = "You are a writer.\nBe vivid, but concise!"
    assert validate_prompt_value(text) == text


def test_describe_settings_includes_prompts_section(db_path: Path) -> None:
    view = describe_settings()
    assert len(view["prompts"]) == 11
    coding = next(p for p in view["prompts"] if p["category"] == "coding")
    assert coding["key"] == "CATEGORY_PROMPT_CODING"
    assert coding["effective_prompt"] == ""
    assert coding["source"] == "default"
    assert coding["default"] == ""


# --- apply_category_role_prompt: unit behavior ----------------------------------


def test_noop_when_category_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forced fast/smart/budget mode (or a caller-forced model) never runs
    the classifier, so RouteDecision.category is "" -- no role prompt should
    ever apply, regardless of configuration."""
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a coder.")
    question, cacheable_system = apply_category_role_prompt("", "hi", "SYSTEM")
    assert question == "hi"
    assert cacheable_system == "SYSTEM"


def test_noop_when_category_has_no_configured_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CATEGORY_PROMPT_CODING", raising=False)
    question, cacheable_system = apply_category_role_prompt("coding", "hi", "SYSTEM")
    assert question == "hi"
    assert cacheable_system == "SYSTEM"


def test_prepends_to_question_when_no_cacheable_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    question, cacheable_system = apply_category_role_prompt("coding", "hi", None)
    assert question == "You are a senior engineer.\n\nhi"
    # No prior stable prefix existed, so the role prompt itself becomes one --
    # a side benefit: a category with a role prompt gets a cacheable prefix
    # even on an otherwise context-free first turn.
    assert cacheable_system == "You are a senior engineer."


def test_prepends_to_both_when_cacheable_system_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    question, cacheable_system = apply_category_role_prompt("coding", "hi", "SYSTEM")
    assert question == "You are a senior engineer.\n\nhi"
    assert cacheable_system == "You are a senior engineer.\n\nSYSTEM"


def test_different_categories_get_different_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a coder.")
    monkeypatch.setenv("CATEGORY_PROMPT_CREATIVE_WRITING", "You are a novelist.")
    _, coding_system = apply_category_role_prompt("coding", "q", "SYSTEM")
    _, writing_system = apply_category_role_prompt("creative_writing", "q", "SYSTEM")
    assert coding_system == "You are a coder.\n\nSYSTEM"
    assert writing_system == "You are a novelist.\n\nSYSTEM"


# --- Caching-prefix stability -----------------------------------------------------


def test_cacheable_prefix_is_byte_identical_across_consecutive_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role prompt is constant per category -- calling it again for a
    later turn of the SAME category (the only thing that changes turn to
    turn is the growing conversation history, which lives in `question`/the
    non-cacheable remainder, never in the role-prompt prefix itself) must
    reproduce byte-identical output, or Anthropic cache_control checkpoints
    and OpenAI's implicit prefix cache would miss every single turn."""
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")

    _, turn1_system = apply_category_role_prompt("coding", "turn one", "SYSTEM BLOCK")
    _, turn2_system = apply_category_role_prompt(
        "coding", "turn two, much longer question", "SYSTEM BLOCK"
    )

    assert turn1_system == turn2_system
    assert turn1_system.startswith("You are a senior engineer.")


def test_cacheable_prefix_unaffected_by_growing_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    short_q, system_a = apply_category_role_prompt("coding", "x", "SYSTEM")
    long_q, system_b = apply_category_role_prompt("coding", "x" * 500, "SYSTEM")
    assert system_a == system_b
    assert short_q != long_q  # only the question side reflects the growth


# --- orchestrator wiring: run_orchestrator / stream_orchestrator ---------------


def test_run_orchestrator_applies_role_prompt_in_auto_mode_for_classified_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    from app.routing import RouteDecision

    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            mode_used="auto->smart",
            model="gpt-5",
            max_output_tokens=4000,
            reasoning_effort="medium",
            notes="",
            category="coding",
        ),
    )

    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="fix this bug", mode=Mode.auto))
    assert seen["kwargs"]["question"].startswith("You are a senior engineer.")


def test_run_orchestrator_skips_role_prompt_for_forced_smart_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode.smart is a forced tier -- no classifier runs, category is "",
    so a configured CATEGORY_PROMPT_CODING must never leak in."""
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="fix this bug", mode=Mode.smart))
    assert "senior engineer" not in seen["kwargs"]["question"]


def test_run_orchestrator_omits_role_prompt_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CATEGORY_PROMPT_CODING", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["kwargs"]["question"] == "hi"


def test_run_orchestrator_role_prompt_precedes_concise_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering: category role prompt -> ... -> concise-mode instruction."""
    from app.orchestrator import _CONCISE_INSTRUCTION
    from app.routing import RouteDecision

    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    monkeypatch.setenv("CONCISE_MODE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            mode_used="auto->smart",
            model="gpt-5",
            max_output_tokens=4000,
            reasoning_effort="medium",
            notes="",
            category="coding",
        ),
    )

    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    orchestrator.run_orchestrator(AskRequest(question="fix this bug", mode=Mode.auto))
    question = seen["kwargs"]["question"]
    assert question.index("You are a senior engineer.") < question.index(
        _CONCISE_INSTRUCTION
    )


def test_stream_orchestrator_applies_role_prompt_for_classified_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routing import RouteDecision

    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "You are a senior engineer.")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            mode_used="auto->smart",
            model="gpt-5",
            max_output_tokens=4000,
            reasoning_effort="medium",
            notes="",
            category="coding",
        ),
    )

    seen: dict = {}

    def fake_stream_model(**kwargs):
        seen["kwargs"] = kwargs
        yield "ok"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    list(
        orchestrator.stream_orchestrator(
            AskRequest(question="fix this bug", mode=Mode.auto)
        )
    )
    assert seen["kwargs"]["question"].startswith("You are a senior engineer.")


# --- HTTP API ----------------------------------------------------------------


def test_put_role_prompt_sets_override_and_persists(client: TestClient) -> None:
    res = client.put(
        "/v1/settings/CATEGORY_PROMPT_CODING",
        json={"value": "You are a senior Python engineer."},
    )
    assert res.status_code == 200
    coding = next(
        p for p in res.json()["prompts"] if p["key"] == "CATEGORY_PROMPT_CODING"
    )
    assert coding["effective_prompt"] == "You are a senior Python engineer."
    assert coding["source"] == "override"

    # Persisted -- a fresh GET reflects it too.
    body = client.get("/v1/settings").json()
    coding = next(p for p in body["prompts"] if p["key"] == "CATEGORY_PROMPT_CODING")
    assert coding["effective_prompt"] == "You are a senior Python engineer."


def test_put_role_prompt_empty_value_clears_override(client: TestClient) -> None:
    client.put(
        "/v1/settings/CATEGORY_PROMPT_CODING", json={"value": "You are a coder."}
    )
    res = client.put("/v1/settings/CATEGORY_PROMPT_CODING", json={"value": ""})
    coding = next(
        p for p in res.json()["prompts"] if p["key"] == "CATEGORY_PROMPT_CODING"
    )
    assert coding["effective_prompt"] == ""
    assert coding["source"] == "default"


def test_put_role_prompt_rejects_oversized_value(client: TestClient) -> None:
    # Caught by SettingUpdate's own schema-level max_length (aligned to
    # MAX_PROMPT_LEN, the largest settable value) before it would even reach
    # validate_prompt_value's own length check -- see that function's direct
    # unit test above for the validator-level rejection itself.
    res = client.put("/v1/settings/CATEGORY_PROMPT_CODING", json={"value": "x" * 4_001})
    assert res.status_code == 422


def test_delete_clears_role_prompt_override(client: TestClient) -> None:
    client.put(
        "/v1/settings/CATEGORY_PROMPT_CODING", json={"value": "You are a coder."}
    )
    res = client.delete("/v1/settings/CATEGORY_PROMPT_CODING")
    coding = next(
        p for p in res.json()["prompts"] if p["key"] == "CATEGORY_PROMPT_CODING"
    )
    assert coding["effective_prompt"] == ""


def test_role_prompt_override_wins_over_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATEGORY_PROMPT_CODING", "env persona")
    client.put(
        "/v1/settings/CATEGORY_PROMPT_CODING", json={"value": "override persona"}
    )
    body = client.get("/v1/settings").json()
    coding = next(p for p in body["prompts"] if p["key"] == "CATEGORY_PROMPT_CODING")
    assert coding["effective_prompt"] == "override persona"
    assert coding["source"] == "override"
    assert coding["env"] == "env persona"
