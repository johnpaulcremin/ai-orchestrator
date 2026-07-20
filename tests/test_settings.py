from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import database
from app.orchestrator import _fallback_models
from app.routing import decide_route
from app.schemas import Mode
from app.settings import (
    SETTABLE_KEYS,
    describe_settings,
    get_model_overrides,
    model_setting,
    settings_writable,
    validate_model_value,
)


class FakeClassifierClient:
    """Stands in for the OpenAI client so auto-routing is deterministic."""

    def __init__(self, output_text: str) -> None:
        result = SimpleNamespace(output_text=output_text)
        self.responses = SimpleNamespace(create=lambda **kwargs: result)

    def with_options(self, **kwargs: object) -> "FakeClassifierClient":
        return self


# --- Resolution precedence: DB override > env var > default ------------------


def test_model_setting_falls_back_to_default(db_path: Path) -> None:
    assert model_setting("OPENAI_MODEL_FAST", "the-default") == "the-default"


def test_model_setting_uses_env(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "env-fast")
    assert model_setting("OPENAI_MODEL_FAST", "the-default") == "env-fast"


def test_db_override_beats_env(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "env-fast")
    database.set_setting("OPENAI_MODEL_FAST", "db-fast")
    assert model_setting("OPENAI_MODEL_FAST", "the-default") == "db-fast"


def test_clearing_override_reverts_to_env(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", "env-fast")
    database.set_setting("OPENAI_MODEL_FAST", "db-fast")
    database.delete_setting("OPENAI_MODEL_FAST")
    assert model_setting("OPENAI_MODEL_FAST", "the-default") == "env-fast"


def test_get_overrides_without_table_is_empty() -> None:
    # No db_path fixture => DATABASE_PATH points at an uninitialised file.
    assert get_model_overrides() == {}


def test_get_overrides_filters_unknown_and_empty_keys(db_path: Path) -> None:
    database.set_setting("OPENAI_MODEL_FAST", "db-fast")
    database.set_setting("NOT_A_SETTABLE_KEY", "nope")
    database.set_setting("OPENAI_MODEL_SMART", "   ")  # whitespace-only => ignored
    overrides = get_model_overrides()
    assert overrides == {"OPENAI_MODEL_FAST": "db-fast"}


# --- Write flag + validation -------------------------------------------------


def test_settings_writable_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_SETTINGS_WRITE", raising=False)
    assert settings_writable() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", "Off"])
def test_settings_writable_can_be_disabled(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_SETTINGS_WRITE", value)
    assert settings_writable() is False


@pytest.mark.parametrize(
    "value",
    [
        "gpt-5",
        "claude-sonnet-5",
        "gemini/gemini-flash-latest",
        "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "groq/llama-3.3-70b-versatile",
    ],
)
def test_validate_accepts_real_model_names(value: str) -> None:
    assert validate_model_value(value) == value


def test_validate_trims_and_treats_blank_as_clear() -> None:
    assert validate_model_value("  gpt-5  ") == "gpt-5"
    assert validate_model_value("   ") == ""


@pytest.mark.parametrize(
    "value",
    ["has space", "semi;colon", "new\nline", "quote'inject", "a" * 201],
)
def test_validate_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        validate_model_value(value)


# --- describe_settings shape -------------------------------------------------


def test_describe_settings_shape(db_path: Path) -> None:
    view = describe_settings()
    assert view["editable"] is True
    assert {t["key"] for t in view["tiers"]} == {
        "OPENAI_MODEL",
        "OPENAI_MODEL_ROUTER",
        "OPENAI_MODEL_BUDGET",
        "OPENAI_MODEL_FAST",
        "OPENAI_MODEL_SMART",
        "OPENAI_MODEL_FALLBACK",
    }
    assert len(view["categories"]) == 11
    coding = next(c for c in view["categories"] if c["category"] == "coding")
    assert coding["tier"] == "smart"
    assert coding["key"] == "MODEL_CODING"
    assert set(coding) >= {
        "key",
        "category",
        "label",
        "tier",
        "effective_model",
        "source",
        "override",
        "inherits",
        "provider",
        "key_env",
        "key_present",
    }


# --- HTTP API ----------------------------------------------------------------


def test_get_settings_endpoint(client: TestClient) -> None:
    body = client.get("/v1/settings").json()
    assert body["editable"] is True
    assert len(body["tiers"]) == 6
    assert len(body["categories"]) == 11


def test_put_sets_override_and_persists(client: TestClient) -> None:
    res = client.put("/v1/settings/MODEL_CODING", json={"value": "claude-sonnet-5"})
    assert res.status_code == 200

    coding = next(c for c in res.json()["categories"] if c["category"] == "coding")
    assert coding["effective_model"] == "claude-sonnet-5"
    assert coding["source"] == "override"
    assert coding["override"] == "claude-sonnet-5"

    # Persisted across a fresh GET.
    reloaded = client.get("/v1/settings").json()
    coding2 = next(c for c in reloaded["categories"] if c["category"] == "coding")
    assert coding2["effective_model"] == "claude-sonnet-5"


def test_put_empty_value_clears_override(client: TestClient) -> None:
    client.put("/v1/settings/MODEL_CODING", json={"value": "claude-sonnet-5"})
    res = client.put("/v1/settings/MODEL_CODING", json={"value": ""})
    coding = next(c for c in res.json()["categories"] if c["category"] == "coding")
    assert coding["source"] != "override"
    assert coding["override"] is None


def test_delete_clears_override(client: TestClient) -> None:
    client.put("/v1/settings/OPENAI_MODEL_SMART", json={"value": "smart-x"})
    res = client.delete("/v1/settings/OPENAI_MODEL_SMART")
    assert res.status_code == 200
    smart = next(t for t in res.json()["tiers"] if t["key"] == "OPENAI_MODEL_SMART")
    assert smart["override"] is None


def test_reset_clears_everything(client: TestClient) -> None:
    client.put("/v1/settings/MODEL_CODING", json={"value": "claude-sonnet-5"})
    client.put("/v1/settings/OPENAI_MODEL_FAST", json={"value": "fast-x"})
    res = client.post("/v1/settings/reset")
    assert res.status_code == 200
    assert all(t["override"] is None for t in res.json()["tiers"])
    assert all(c["override"] is None for c in res.json()["categories"])


def test_put_rejects_unknown_key(client: TestClient) -> None:
    res = client.put("/v1/settings/OPENAI_API_KEY", json={"value": "sk-leak"})
    assert res.status_code == 400
    # A credential key must never be settable through this API.
    assert "OPENAI_API_KEY" not in SETTABLE_KEYS


def test_put_rejects_malformed_value(client: TestClient) -> None:
    res = client.put("/v1/settings/MODEL_CODING", json={"value": "has space"})
    assert res.status_code == 400


def test_writes_blocked_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_SETTINGS_WRITE", "false")
    assert client.get("/v1/settings").json()["editable"] is False
    assert (
        client.put("/v1/settings/MODEL_CODING", json={"value": "x"}).status_code == 403
    )
    assert client.delete("/v1/settings/MODEL_CODING").status_code == 403
    assert client.post("/v1/settings/reset").status_code == 403


def test_settings_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/settings").status_code == 401
    assert (
        client.put("/v1/settings/MODEL_CODING", json={"value": "x"}).status_code == 401
    )
    ok = client.get("/v1/settings", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200


def test_fallback_chain_includes_base_code_default(db_path: Path) -> None:
    # With no OPENAI_MODEL / FAST / FALLBACK configured (all cleared by the
    # autouse fixture), the base "gpt-5" code default must still be offered as a
    # fallback candidate, so overriding only a tier can't empty the chain.
    assert _fallback_models("claude-opus-x") == ["gpt-5"]


def test_routing_honours_saved_override(client: TestClient) -> None:
    # Save a category override through the API, then confirm the router uses it.
    client.put("/v1/settings/MODEL_CODING", json={"value": "claude-sonnet-5"})

    fake = FakeClassifierClient(
        '{"category": "coding", "complexity": "medium", "reason": "code"}'
    )
    decision = decide_route("write a function", Mode.auto, client=fake)

    assert decision.model == "claude-sonnet-5"
    assert decision.mode_used == "auto->smart:coding"
