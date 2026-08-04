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
        "do you support image generation",
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
    for term in ("LiteLLM", "SQLite", "vector database", "Ollama", "cached"):
        assert term in snapshot["internals"]


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
