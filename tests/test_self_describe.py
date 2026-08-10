"""Self-description (app/self_describe.py): the phrase heuristic,
capabilities_snapshot()'s accuracy against real configured state,
orchestrator gating/composition, GET /v1/capabilities, and the
anti-confabulation guarantee this feature actually provides — the appended
note always carries the REAL configured model/flags/limits, regardless of
what the answering model's own text says.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import orchestrator_extract, orchestrator_tools, providers, self_describe
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode


# --- config / flags -----------------------------------------------------------


def test_self_describe_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SELF_DESCRIBE", raising=False)
    assert self_describe.self_describe_enabled() is False
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    assert self_describe.self_describe_enabled() is True
    monkeypatch.setenv("SELF_DESCRIBE", "false")
    assert self_describe.self_describe_enabled() is False


# --- looks_like_capabilities_request --------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "what can you do?",
        "what models do you use for coding",
        "what do you support in terms of image generation",
        "what features do you support",
        "what are your capabilities",
        "how much budget do I have left",
        "tell me about yourself",
    ],
)
def test_looks_like_capabilities_request_matches(question: str) -> None:
    assert self_describe.looks_like_capabilities_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "what's the capital of France?",
        "write me a poem about autumn",
        "help me debug this Python function",
        "what do dogs eat",
    ],
)
def test_looks_like_capabilities_request_no_match(question: str) -> None:
    assert self_describe.looks_like_capabilities_request(question) is False


# --- must-not-fire traps: the same class of bug the fact_check phrase-list
# post-mortem found (a bare/generic fragment that fires on an unrelated
# sentence). See _SELF_DESCRIBE_PHRASES's comment for which three phrases
# were removed and why each of these would have false-positived on the
# REMOVED phrase specifically.
@pytest.mark.parametrize(
    "question",
    [
        # would have matched the removed bare "what are you" fragment
        "what are you doing with that variable in the loop?",
        "what are you thinking for the next step?",
        "what are you talking about?",
        # would have matched the removed bare "what version of" fragment
        "what version of Python should I use for this?",
        "what version of Node do I need for this package?",
        # would have matched the removed bare "do you support" fragment
        "do you support my decision to refactor this?",
        "do you support universal healthcare?",
    ],
)
def test_looks_like_capabilities_request_no_match_removed_phrase_traps(
    question: str,
) -> None:
    assert self_describe.looks_like_capabilities_request(question) is False


# --- must-not-fire traps: meta-questions about a previous answer, and
# general AI questions not about this app. The phrase heuristic never had a
# false-positive risk here (no shared substrings with any positive phrase),
# but these are pinned so a future phrase addition can't silently reopen it.
@pytest.mark.parametrize(
    "question",
    [
        "which model answered that?",
        "why did that take two attempts?",
        "why did it fail?",
        "what took so long just now?",
        "why did you need to retry?",
        "what's the best coding model right now?",
        "how do transformers work?",
        "what is a large language model?",
    ],
)
def test_looks_like_capabilities_request_no_match_meta_and_general_ai_traps(
    question: str,
) -> None:
    assert self_describe.looks_like_capabilities_request(question) is False


# --- capabilities_snapshot: accuracy against real configured state -------------


def test_snapshot_reports_the_real_configured_fast_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert (
        snapshot["models"]["tiers"]["OPENAI_MODEL_FAST"] == "gemini/gemini-flash-latest"
    )


def test_snapshot_reports_the_real_enabled_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.delenv("FACT_CHECK", raising=False)
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["flags"]["MATH_SOLVE"] is True
    assert snapshot["flags"]["FACT_CHECK"] is False


def test_snapshot_disabled_features_lists_off_flags_with_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setenv("FACT_CHECK", "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    keys = {f["key"] for f in snapshot["disabled_features"]}
    assert "FACT_CHECK" in keys
    assert "MATH_SOLVE" not in keys
    fact_check = next(
        f for f in snapshot["disabled_features"] if f["key"] == "FACT_CHECK"
    )
    assert fact_check["purpose"]  # non-empty one-line description


def test_snapshot_disabled_features_empty_when_everything_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.settings import FEATURE_FLAG_KEYS

    for key in FEATURE_FLAG_KEYS:
        monkeypatch.setenv(key, "true")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["disabled_features"] == []


def test_snapshot_version_matches_module_constant() -> None:
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["version"] == self_describe.APP_VERSION


def test_snapshot_omits_budget_figures_when_no_per_owner_cap_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    snapshot = self_describe.capabilities_snapshot(owner="alice")
    assert snapshot["budget"]["daily_budget_per_owner_usd"] is None
    assert snapshot["budget"]["owner_remaining_usd"] is None


def test_snapshot_reports_real_owner_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    from app import database

    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "5")
    database.record_spend("alice", "gpt-5", 100, 100, 1.5)
    snapshot = self_describe.capabilities_snapshot(owner="alice")
    assert snapshot["budget"]["daily_budget_per_owner_usd"] == 5.0
    assert snapshot["budget"]["owner_remaining_usd"] == pytest.approx(3.5)


def test_snapshot_free_lane_empty_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FREE_TIER_MODELS", raising=False)
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["free_lane"] == {"enabled": False, "models": []}


def test_snapshot_free_lane_reports_real_quota_status(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    monkeypatch.setenv("FREE_TIER_MODELS", "gemini/gemini-flash-latest")
    monkeypatch.setenv("FREE_TIER_DEFAULT_QUOTA", "10")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["free_lane"]["enabled"] is True
    assert snapshot["free_lane"]["models"] == [
        {
            "model": "gemini/gemini-flash-latest",
            "quota": 10,
            "used": 0,
            "remaining": 10,
        }
    ]


def test_snapshot_includes_static_internals_summary() -> None:
    """See self_describe.INTERNALS_SUMMARY's docstring: this app already
    runs on LiteLLM/a RAG library/SQLite, so a model asked to suggest
    improvements needs this in the payload to avoid re-proposing them."""
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["internals"] == self_describe.INTERNALS_SUMMARY
    for term in (
        "LiteLLM",
        "SQLite",
        "vector database",
        "Ollama",
        "cached",
        "workflow",
    ):
        assert term in snapshot["internals"]


def test_snapshot_includes_ui_capabilities() -> None:
    """See self_describe._ui_capabilities: INTERNALS_SUMMARY covers how the
    app is BUILT and _flags() gives bare flag names, so nothing told the
    model what the interface can actually DO -- and it filled the gap by
    inventing, including "improvements" that already ship."""
    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["ui"] == self_describe._ui_capabilities()
    for term in ("markdown", "share link", "badges", "branched", "home screen"):
        assert term in snapshot["ui"]


def test_ui_capabilities_claims_a_feature_only_while_it_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION", "true")
    assert ".xlsx/.csv previews inline" in self_describe._ui_capabilities()

    monkeypatch.setenv("CODE_EXECUTION", "false")
    assert ".xlsx/.csv previews inline" not in self_describe._ui_capabilities()


def test_ui_capabilities_claims_nothing_optional_when_everything_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard version of the rule: with every flag off, the paragraph must
    reduce to capabilities that are unconditionally true of the interface,
    and make no claim whatsoever about the optional ones."""
    from app.settings import FEATURE_FLAG_KEYS

    for key in FEATURE_FLAG_KEYS:
        monkeypatch.setenv(key, "false")
    text = self_describe._ui_capabilities()

    assert "markdown" in text  # unconditional, still claimed
    assert "With the features currently enabled" not in text
    for absent in (
        "generated images display inline",
        ".xlsx/.csv previews inline",
        "search queries that were issued",
        "checked claim",
        "computed expression",
        "scholarly works",
        "past conversations an answer drew on",
        "document library panel",
    ):
        assert absent not in text


def test_ui_capabilities_never_claims_a_disabled_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-checked against _disabled_features() itself rather than a
    hardcoded list, so a newly flag-gated clause cannot quietly skip the
    rule."""
    from app.settings import FEATURE_FLAG_KEYS

    for index, key in enumerate(FEATURE_FLAG_KEYS):
        # An arbitrary half-on/half-off configuration, so the assertion is
        # about the gating and not about "everything off".
        monkeypatch.setenv(key, "true" if index % 2 else "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    off = {f["key"] for f in snapshot["disabled_features"]}
    clauses = dict(self_describe._UI_FLAGGED)

    for key in off & clauses.keys():
        assert clauses[key] not in snapshot["ui"], f"{key} is off but still claimed"
    for key in clauses.keys() - off:
        assert clauses[key] in snapshot["ui"], f"{key} is on but not claimed"


def test_limits_reports_real_schema_constants() -> None:
    from app.schemas import _MAX_INPUT_IMAGES, _MAX_QUESTION_CHARS

    snapshot = self_describe.capabilities_snapshot(owner=None)
    assert snapshot["limits"]["max_question_chars"] == _MAX_QUESTION_CHARS
    assert snapshot["limits"]["max_attached_images"] == _MAX_INPUT_IMAGES


# --- format_note -----------------------------------------------------------------


def test_format_note_includes_identity_and_version() -> None:
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "ai-orchestrator" in note
    assert self_describe.APP_VERSION in note


def test_format_note_includes_internals_summary() -> None:
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert self_describe.INTERNALS_SUMMARY in note


def test_format_note_includes_ui_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The assembled SELF_DESCRIBE context -- what actually reaches the model
    -- carries the UI facts, not just the snapshot JSON."""
    monkeypatch.setenv("CODE_EXECUTION", "true")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert self_describe._ui_capabilities() in note
    assert "markdown" in note
    assert ".xlsx/.csv previews inline" in note


def test_format_note_does_not_claim_a_disabled_ui_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switched-off feature is reported as available-but-off, and is NEVER
    also described as something the interface does."""
    monkeypatch.setenv("CODE_EXECUTION", "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert ".xlsx/.csv previews inline" not in note
    assert "the code that was run" not in note
    # ...and it is still surfaced as available-but-off, so the model can say
    # "that would have helped here" rather than silently doing without.
    assert "CODE_EXECUTION" in note
    assert "Available but off" in note


def test_format_note_lists_disabled_features_with_purpose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "FACT_CHECK" in note
    assert "Available but off" in note
    assert "owner can enable" in note


def test_format_note_omits_disabled_features_line_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.settings import FEATURE_FLAG_KEYS

    for key in FEATURE_FLAG_KEYS:
        monkeypatch.setenv(key, "true")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "Available but off" not in note


def test_format_note_lists_enabled_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "MATH_SOLVE" in note


def test_format_note_says_none_when_no_flags_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.settings import describe_settings

    for key in describe_settings()["features"]:
        monkeypatch.delenv(key["key"], raising=False)
        monkeypatch.setenv(key["key"], "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "Enabled optional features — none" in note


# --- tool description: pins the negative ("do not call for...") guidance so
# it can't silently drift back to the misfire this was written to prevent --
# the model reads this text to DECIDE whether to call the tool, so the
# wording itself is the actual trigger-tightening mechanism on this path
# (see the module's PR/CHANGELOG note on the meta-question misfire).


def test_tool_description_tells_the_model_not_to_call_for_a_prior_answer() -> None:
    text = self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION
    assert "Do NOT call this for a question about a SPECIFIC PREVIOUS answer" in text
    assert "which model answered that" in text
    assert "why did that take two attempts" in text
    assert "why did it fail" in text
    assert "no memory of past turns" in text


def test_tool_description_tells_the_model_not_to_call_for_general_ai_questions() -> (
    None
):
    text = self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION
    assert "general question about AI/LLMs that isn't about this specific app" in text


def test_tool_description_says_call_only_for_a_direct_question_about_the_app() -> None:
    assert (
        "Call this ONLY for a direct question about the app itself"
        in self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION
    )


# --- tool schema: both provider paths ---------------------------------------


def test_build_self_describe_tool_openai_shape() -> None:
    tool = orchestrator_tools._build_self_describe_tool()["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "app_capabilities"
    assert tool["description"] == self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION
    assert tool["parameters"] == self_describe.app_capabilities_input_schema()


def test_anthropic_self_describe_tool_shape() -> None:
    tool = providers._anthropic_self_describe_tool()
    assert tool["name"] == "app_capabilities"
    assert tool["description"] == self_describe.APP_CAPABILITIES_TOOL_DESCRIPTION
    assert tool["input_schema"] == self_describe.app_capabilities_input_schema()


def test_build_tools_includes_self_describe_when_capabilities_true() -> None:
    tools = orchestrator_tools._build_tools(
        web_search=False, actions=False, capabilities=True
    )["tools"]
    assert any(t.get("name") == "app_capabilities" for t in tools)


def test_anthropic_tools_includes_self_describe_when_capabilities_true() -> None:
    tools = providers._anthropic_tools(
        web_search=False, actions=False, capabilities=True
    )
    assert tools is not None
    assert any(t.get("name") == "app_capabilities" for t in tools)


# --- extraction: both provider paths -----------------------------------------


class _FakeFunctionCall:
    def __init__(self, name: str) -> None:
        self.type = "function_call"
        self.name = name


class _FakeResult:
    def __init__(self, output: list[object]) -> None:
        self.output = output


def test_extract_capabilities_call_true_for_a_matching_call() -> None:
    result = _FakeResult([_FakeFunctionCall("app_capabilities")])
    assert orchestrator_extract._extract_capabilities_call(result) is True


def test_extract_capabilities_call_ignores_other_function_calls() -> None:
    result = _FakeResult([_FakeFunctionCall("math_solve")])
    assert orchestrator_extract._extract_capabilities_call(result) is False


def test_extract_capabilities_call_no_output_attr_tolerated() -> None:
    assert orchestrator_extract._extract_capabilities_call(object()) is False


class _FakeToolUseBlock:
    def __init__(self, name: str) -> None:
        self.type = "tool_use"
        self.name = name


class _FakeAnthropicMessage:
    def __init__(self, content: list[object]) -> None:
        self.content = content


def test_extract_anthropic_capabilities_call_true_for_a_matching_block() -> None:
    message = _FakeAnthropicMessage([_FakeToolUseBlock("app_capabilities")])
    assert providers._extract_anthropic_capabilities_call(message) is True


def test_extract_anthropic_capabilities_call_ignores_other_tool_names() -> None:
    message = _FakeAnthropicMessage([_FakeToolUseBlock("math_solve")])
    assert providers._extract_anthropic_capabilities_call(message) is False


def test_extract_anthropic_capabilities_call_no_content_attr_tolerated() -> None:
    assert providers._extract_anthropic_capabilities_call(object()) is False


# --- orchestrator: gating (tool offered vs heuristic fallback) ---------------


def test_run_orchestrator_offers_the_tool_when_enabled_for_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["capabilities"] = kwargs["capabilities"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    # Any question — the tool is offered regardless of phrase content; the
    # MODEL decides whether to call it, not a heuristic on the app's side.
    run_orchestrator(AskRequest(question="what's the weather", mode=Mode.smart))
    assert seen["capabilities"] is True


def test_run_orchestrator_offers_the_tool_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["capabilities"] = kwargs["capabilities"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["capabilities"] is True


def test_run_orchestrator_tool_not_offered_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SELF_DESCRIBE", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["capabilities"] = kwargs["capabilities"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))
    assert seen["capabilities"] is False


def test_run_orchestrator_tool_not_offered_for_litellm_falls_back_to_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LiteLLM-routed model (Gemini here) has no native tool-calling wired
    up (see orchestrator._SELF_DESCRIBE_TOOL_PROVIDERS) — the tool is never
    offered, but the phrase heuristic still triggers the note directly."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["capabilities"] = kwargs["capabilities"]
        return "I don't have any specific models — I'm just a generic assistant."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.fast)
    )
    assert seen["capabilities"] is False
    assert "gemini/gemini-flash-latest" in result.answer


def test_run_orchestrator_litellm_heuristic_requires_the_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="what's the weather", mode=Mode.fast))
    assert result.answer == "ok"


# --- orchestrator: the tool actually being called + anti-confabulation ------


def test_run_orchestrator_no_note_when_the_tool_is_offered_but_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offering the tool alone must not append the note — only an actual
    call (the model deciding this question is really about the app) does."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))
    assert result.answer == "ok"


def test_run_orchestrator_appends_real_data_even_when_model_confabulates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core anti-confabulation guarantee: even when the stubbed model's
    own answer text asserts something false about the app, the appended
    note still carries the REAL configured model name — the ground truth
    is never lost to whatever the model's own prose claims. `_call_model`
    is stubbed to populate `capabilities_calls`, simulating the model
    actually calling the app_capabilities tool (see orchestrator_calls.py's
    real dispatch, which does this via _extract_capabilities_call)."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "I don't have any specific models — I'm just a generic assistant."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.smart)
    )

    assert "I don't have any specific models" in result.answer
    assert "gpt-5" in result.answer
    assert "Verified capabilities" in result.answer


def test_run_orchestrator_skips_cache_when_the_tool_was_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))

    key = cache.make_key("what can you do?", "smart")
    assert cache.get(key) is None


def test_run_orchestrator_caches_when_the_tool_was_offered_but_not_called(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    run_orchestrator(AskRequest(question="ordinary question", mode=Mode.smart))

    key = cache.make_key("ordinary question", "smart")
    assert cache.get(key) is not None


def test_stream_orchestrator_appends_real_data_when_the_tool_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs: object):
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        yield "I have no models."

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(
            AskRequest(question="what models do you use?", mode=Mode.smart)
        )
    )
    done = events[-1]
    assert done["event"] == "done"
    assert "gpt-5" in done["data"]["answer"]


def test_stream_orchestrator_no_note_when_the_tool_is_offered_but_not_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    events = list(
        stream_orchestrator(
            AskRequest(question="what models do you use?", mode=Mode.smart)
        )
    )
    done = events[-1]
    assert done["data"]["answer"] == "ok"


def test_stream_orchestrator_heuristic_fallback_for_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **_kw: iter(["I have no models."])
    )

    events = list(
        stream_orchestrator(
            AskRequest(question="what models do you use?", mode=Mode.fast)
        )
    )
    done = events[-1]
    assert done["event"] == "done"
    assert "gemini/gemini-flash-latest" in done["data"]["answer"]


def test_stream_orchestrator_no_note_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SELF_DESCRIBE", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))

    events = list(
        stream_orchestrator(
            AskRequest(question="what models do you use?", mode=Mode.fast)
        )
    )
    done = events[-1]
    assert done["data"]["answer"] == "ok"


# --- HTTP: GET /v1/capabilities ---------------------------------------------------


def test_capabilities_endpoint_returns_the_real_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == self_describe.APP_VERSION
    assert body["internals"] == self_describe.INTERNALS_SUMMARY
    assert body["models"]["tiers"]["OPENAI_MODEL_FAST"] == "gemini/gemini-flash-latest"
    assert "flags" in body
    assert "disabled_features" in body
    assert any(f["key"] == "MATH_SOLVE" for f in body["disabled_features"])
    assert "limits" in body
    assert "budget" in body
    assert "free_lane" in body


def test_capabilities_endpoint_budget_is_scoped_by_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact privacy boundary GET /v1/usage already enforces: each
    owner's remaining budget is their OWN spend, never mixed with (or
    leaked from) another owner's, and never the live global total (see
    self_describe._owner_budget's docstring)."""
    from app import database

    monkeypatch.setenv("JWT_SECRET", "capabilities-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "10")

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )
    bob = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "password123"}
    ).json()["access_token"]

    database.record_spend("alice", "gpt-5", 100, 100, 4.0)

    alice_body = client.get(
        "/v1/capabilities", headers={"Authorization": f"Bearer {alice}"}
    ).json()
    bob_body = client.get(
        "/v1/capabilities", headers={"Authorization": f"Bearer {bob}"}
    ).json()

    assert alice_body["budget"]["owner_remaining_usd"] == pytest.approx(6.0)
    assert bob_body["budget"]["owner_remaining_usd"] == pytest.approx(10.0)


def test_capabilities_endpoint_reflects_a_live_settings_flag_flip(
    client: TestClient,
) -> None:
    """Not just an env var — a saved Settings override (the same path the
    Settings panel's toggle uses, override > env > default) is reflected
    live, with no restart, exactly the same way GET /v1/settings itself
    already does."""
    before = client.get("/v1/capabilities").json()
    assert before["flags"]["MATH_SOLVE"] is False

    res = client.put("/v1/settings/MATH_SOLVE", json={"value": "true"})
    assert res.status_code == 200

    after = client.get("/v1/capabilities").json()
    assert after["flags"]["MATH_SOLVE"] is True


# --- Settings registry -----------------------------------------------------------


def test_self_describe_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "SELF_DESCRIBE")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False


# --- cacheable system prefix: static identity line, no live numbers ---------


def test_identity_line_absent_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.context_builder import build_context_prompt_with_cache_split

    monkeypatch.delenv("SELF_DESCRIBE", raising=False)
    _full, cacheable_system, _remainder = build_context_prompt_with_cache_split(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        "current question",
        system_prompt="Be concise.",
    )
    assert cacheable_system is not None
    assert self_describe.CAPABILITIES_IDENTITY_LINE not in cacheable_system


def test_identity_line_present_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.context_builder import build_context_prompt_with_cache_split

    monkeypatch.setenv("SELF_DESCRIBE", "true")
    _full, cacheable_system, _remainder = build_context_prompt_with_cache_split(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        "current question",
        system_prompt="Be concise.",
    )
    assert cacheable_system is not None
    assert self_describe.CAPABILITIES_IDENTITY_LINE in cacheable_system
    # Static hint only — no live figures (a remaining-budget number, a
    # model name) ever baked into the cacheable prefix itself.
    assert "$" not in self_describe.CAPABILITIES_IDENTITY_LINE
    # The internals paragraph is appended-note-only (format_note), never
    # folded into the cacheable prefix itself.
    assert self_describe.INTERNALS_SUMMARY not in cacheable_system


def test_the_identity_line_never_orders_a_tool_call_it_cannot_guarantee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed live: an Ollama budget-tier turn failed over to Claude, which
    got this line but not the tool, and wrote a made-up text invocation of it
    into the answer body where the answer should have been.

    The line is assembled before routing picks a model, so it can never know
    whether the answering model was offered the tool — the wording has to
    carry that uncertainty instead."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    line = self_describe.CAPABILITIES_IDENTITY_LINE

    assert "if it is among the tools available to you" in line
    assert "never write a tool call out as text" in line
    # The order it used to give, unconditionally.
    assert "limits, call the app_capabilities tool." not in line


def test_a_litellm_model_gets_the_identity_line_but_never_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The structural mismatch the wording exists to survive, pinned on both
    sides at once: the prompt carries the hint, and the gate refuses the tool.

    Neither half is a bug on its own — the prefix is deliberately static and
    model-blind for prompt-cache stability, and a LiteLLM model genuinely has
    no native tool wired up. It is the COMBINATION a tool-less model has to be
    told how to handle."""
    from app.context_builder import build_context_prompt_with_cache_split

    monkeypatch.setenv("SELF_DESCRIBE", "true")
    _full, cacheable_system, _remainder = build_context_prompt_with_cache_split(
        [], "what can you do?"
    )
    assert self_describe.CAPABILITIES_IDENTITY_LINE in (cacheable_system or "")

    req = AskRequest(question="what can you do?")
    # Index 7 is self_describe_tool_wanted — see _tool_flags_for's docstring.
    assert orchestrator._tool_flags_for("ollama/llama3.1:8b", req, False)[7] is False
    # The contrast, so this can never pass just because the flag was off: the
    # SAME prefix reaches a model that IS offered the tool.
    assert orchestrator._tool_flags_for("claude-sonnet-5", req, False)[7] is True


def test_identity_line_present_for_a_brand_new_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no prior history and no custom instructions — the shape
    self_describe.py's docstring specifically calls out as needing the
    identity line to actually reach the model on a first turn."""
    from app.context_builder import build_context_prompt_with_cache_split

    monkeypatch.setenv("SELF_DESCRIBE", "true")
    _full, cacheable_system, _remainder = build_context_prompt_with_cache_split(
        [], "what can you do?"
    )
    assert cacheable_system == self_describe.CAPABILITIES_IDENTITY_LINE


def test_prefix_stays_byte_identical_across_consecutive_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of isolating a cacheable prefix (see
    context_builder.build_context_prompt_with_cache_split's docstring):
    the SAME system_prompt/history produces the SAME cacheable_system text
    regardless of what the current turn's own question is — a provider's
    native prompt caching (or OpenAI's automatic prefix caching) only pays
    off when that prefix is byte-identical turn over turn."""
    from app.context_builder import build_context_prompt_with_cache_split

    monkeypatch.setenv("SELF_DESCRIBE", "true")
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    _full_1, cacheable_1, _r1 = build_context_prompt_with_cache_split(
        history, "first turn's question", system_prompt="Be concise."
    )
    _full_2, cacheable_2, _r2 = build_context_prompt_with_cache_split(
        history,
        "a completely different second turn's question",
        system_prompt="Be concise.",
    )
    assert cacheable_1 == cacheable_2
    assert self_describe.CAPABILITIES_IDENTITY_LINE in (cacheable_1 or "")


# --- capabilities snapshots must never reach cross-conversation memory -------
#
# The note format_note builds carries live per-owner account state: the
# effective model map, enabled flags, request limits, free-lane quotas, and
# the owner's REMAINING DAILY BUDGET IN USD. The response cache has always
# refused to store it (orchestrator's `cacheable_answer`); memory had no
# equivalent guard and, unlike the cache, no TTL.


def test_run_orchestrator_marks_a_tool_called_answer_unmemorable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "I have no models."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.smart)
    )
    assert "gpt-5" in result.answer  # the snapshot really was appended
    assert result.memorable is False


def test_run_orchestrator_marks_a_heuristic_note_answer_unmemorable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LiteLLM path reaches the same note by a different route (phrase
    heuristic, no native tool), so it needs its own guard."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "generic answer")

    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.fast)
    )
    assert "gemini/gemini-flash-latest" in result.answer
    assert result.memorable is False


def test_an_ordinary_answer_stays_memorable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The converse, so the guard can't quietly become 'never remember'."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Paris.")

    result = run_orchestrator(
        AskRequest(question="what is the capital of France?", mode=Mode.smart)
    )
    assert result.memorable is True


def test_memorable_never_reaches_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is an orchestrator -> ask-route signal, not client-facing: excluded
    from serialization so it stays out of the API response and the OpenAPI
    schema."""
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")
    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert "memorable" not in result.model_dump()
    assert "memorable" not in result.model_dump_json()


def test_stream_orchestrator_flags_a_capabilities_answer_and_hides_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming twin. The done frame carries the signal for the persistence
    worker, which pops it — so the SSE contract the client sees is unchanged
    (asserted in the worker test below)."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs: object):
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        yield "I have no models."

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)
    events = list(
        stream_orchestrator(
            AskRequest(question="what models do you use?", mode=Mode.smart)
        )
    )
    assert events[-1]["data"]["memorable"] is False


def test_stream_orchestrator_omits_the_flag_for_an_ordinary_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["ok"]))
    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    assert "memorable" not in events[-1]["data"]


# --- a textless tool call answers the question, not just the config ----------
#
# Both providers end a tool-calling turn on the tool_use block, awaiting a
# result this codebase never sends back (see app/self_describe.py's module
# docstring). So "the model called app_capabilities and wrote nothing" is the
# ORDINARY shape, not an edge case — and folding the note in as the whole
# answer meant the user got a configuration listing instead of an answer. Two
# genuinely different questions ("how is this better than other apps?", "what
# makes it weaker?") came back with the identical dump, which is what prompted
# this.


def _tool_only_then(second_answer: str):
    """First call: the tool fires, no prose (the real-world shape). Second
    call: the grounded follow-up, which sees the facts in its prompt."""
    calls: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        calls.append(str(kwargs["question"]))
        if len(calls) == 1:
            kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
            return ""
        return second_answer

    return fake_call_model, calls


def test_textless_tool_call_answers_the_question_instead_of_dumping_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    fake, calls = _tool_only_then("It routes per-question, which most apps don't.")
    monkeypatch.setattr(orchestrator, "_call_model", fake)

    result = run_orchestrator(
        AskRequest(question="how is this better than other apps?", mode=Mode.smart)
    )

    assert result.answer == "It routes per-question, which most apps don't."
    assert "Verified capabilities" not in result.answer  # not the dump
    assert "grounded self-describe" in result.notes  # the 2nd call is disclosed

    # The follow-up carried the real facts AND the original question.
    assert len(calls) == 2
    assert "how is this better than other apps?" in calls[1]
    assert "gpt-5" in calls[1]


def test_grounded_followup_runs_with_every_tool_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-offering app_capabilities would produce a second textless turn, and
    with it an unbounded loop."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen: list[dict[str, object]] = []

    def fake_call_model(**kwargs: object) -> str:
        seen.append(dict(kwargs))
        if len(seen) == 1:
            kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
            return ""
        return "grounded answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))

    assert seen[1].get("capabilities") in (False, None)
    assert seen[1].get("web_search") in (False, None)
    assert seen[1].get("code_execution") in (False, None)


def test_falls_back_to_the_note_when_the_followup_also_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never worse than the old behaviour: an empty follow-up still leaves the
    verified facts as the answer rather than nothing at all."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    fake, _calls = _tool_only_then("")
    monkeypatch.setattr(orchestrator, "_call_model", fake)

    result = run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))

    assert "Verified capabilities" in result.answer
    assert "grounded self-describe" not in result.notes


def test_no_followup_when_the_model_already_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anti-confabulation append (see
    test_run_orchestrator_appends_real_data_even_when_model_confabulates) is
    untouched — and costs no second call."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    calls: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        calls.append(str(kwargs["question"]))
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return "I use magic beans."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    result = run_orchestrator(AskRequest(question="what models?", mode=Mode.smart))

    assert len(calls) == 1  # no second call
    assert "I use magic beans." in result.answer
    assert "Verified capabilities" in result.answer  # ground truth still wins


def test_stream_textless_tool_call_answers_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs: object):
        kwargs["capabilities_calls"].append(True)  # type: ignore[union-attr]
        return iter(())

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "a real answer")

    events = list(
        stream_orchestrator(AskRequest(question="what makes it weak?", mode=Mode.smart))
    )
    done = next(e["data"] for e in events if e["event"] == "done")

    assert done["answer"] == "a real answer"
    assert "Verified capabilities" not in str(done["answer"])
    assert "grounded self-describe" in str(done["notes"])


def test_grounded_question_keeps_the_facts_and_forbids_a_listing() -> None:
    built = self_describe.grounded_question("why is this slow?", "- Models — x: y")
    assert "why is this slow?" in built
    assert "- Models — x: y" in built
    assert "do NOT" in built  # the instruction that replaces the dump
