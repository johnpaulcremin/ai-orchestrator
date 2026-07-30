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
from app import self_describe
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


def test_format_note_lists_enabled_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "MATH_SOLVE" in note


def test_format_note_says_none_when_no_flags_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in self_describe.describe_settings()["features"]:
        monkeypatch.delenv(key["key"], raising=False)
        monkeypatch.setenv(key["key"], "false")
    snapshot = self_describe.capabilities_snapshot(owner=None)
    note = self_describe.format_note(snapshot)
    assert "Enabled optional features — none" in note


# --- orchestrator: gating + anti-confabulation -----------------------------------


def test_run_orchestrator_self_describe_wanted_requires_enabled_and_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_snapshot(owner):
        seen["called"] = True
        return self_describe.capabilities_snapshot(owner)

    monkeypatch.setattr(orchestrator, "capabilities_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    # Disabled entirely: heuristic matches, but SELF_DESCRIBE is off.
    monkeypatch.delenv("SELF_DESCRIBE", raising=False)
    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))
    assert "called" not in seen

    # Enabled, but the question doesn't look like a capabilities request.
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    run_orchestrator(AskRequest(question="what's the weather", mode=Mode.smart))
    assert "called" not in seen

    # Enabled AND matches the heuristic.
    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))
    assert seen.get("called") is True


def test_run_orchestrator_appends_real_data_even_when_model_confabulates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core anti-confabulation guarantee: even when the stubbed model's
    own answer text asserts something false about the app, the appended
    note still carries the REAL configured model name — the ground truth
    is never lost to whatever the model's own prose claims."""
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **_kw: (
            "I don't have any specific models — I'm just a generic assistant."
        ),
    )

    result = run_orchestrator(
        AskRequest(question="what models do you use?", mode=Mode.fast)
    )

    assert "I don't have any specific models" in result.answer
    assert "gemini/gemini-flash-latest" in result.answer
    assert "Verified capabilities" in result.answer


def test_run_orchestrator_skips_cache_when_self_describe_wanted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("SELF_DESCRIBE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    run_orchestrator(AskRequest(question="what can you do?", mode=Mode.smart))

    key = cache.make_key("what can you do?", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_appends_real_data_to_the_streamed_answer(
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
    assert body["models"]["tiers"]["OPENAI_MODEL_FAST"] == "gemini/gemini-flash-latest"
    assert "flags" in body
    assert "limits" in body
    assert "budget" in body
    assert "free_lane" in body


# --- Settings registry -----------------------------------------------------------


def test_self_describe_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "SELF_DESCRIBE")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
