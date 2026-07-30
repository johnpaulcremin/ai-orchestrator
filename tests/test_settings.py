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
    FEATURE_FLAG_KEYS,
    SETTABLE_KEYS,
    bool_setting,
    describe_settings,
    get_model_overrides,
    model_setting,
    settings_writable,
    validate_bool_value,
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


# --- Feature-flag resolution: bool_setting/validate_bool_value ---------------


def test_bool_setting_falls_back_to_default(db_path: Path) -> None:
    assert bool_setting("WEB_SEARCH", False) is False
    assert bool_setting("WEB_SEARCH", True) is True


def test_bool_setting_uses_env(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    assert bool_setting("WEB_SEARCH", False) is True


def test_bool_setting_db_override_beats_env(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    database.set_setting("WEB_SEARCH", "false")
    assert bool_setting("WEB_SEARCH", False) is False


def test_bool_setting_clearing_override_reverts_to_env(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_SEARCH", "true")
    database.set_setting("WEB_SEARCH", "false")
    database.delete_setting("WEB_SEARCH")
    assert bool_setting("WEB_SEARCH", False) is True


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE", "On"])
def test_validate_bool_accepts_truthy_spellings(value: str) -> None:
    assert validate_bool_value(value) == "true"


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", "Off"])
def test_validate_bool_accepts_falsy_spellings(value: str) -> None:
    assert validate_bool_value(value) == "false"


def test_validate_bool_treats_blank_as_clear() -> None:
    assert validate_bool_value("   ") == ""


def test_validate_bool_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        validate_bool_value("banana")


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
    assert {f["key"] for f in view["features"]} == set(FEATURE_FLAG_KEYS)
    code_execution = next(f for f in view["features"] if f["key"] == "CODE_EXECUTION")
    assert code_execution["effective_enabled"] is False
    assert code_execution["source"] == "default"
    assert set(code_execution) >= {
        "key",
        "label",
        "description",
        "effective_enabled",
        "source",
        "override",
        "env",
        "default",
    }


# --- HTTP API ----------------------------------------------------------------


def test_get_settings_endpoint(client: TestClient) -> None:
    body = client.get("/v1/settings").json()
    assert body["editable"] is True
    assert len(body["tiers"]) == 6
    assert len(body["categories"]) == 11
    assert len(body["features"]) == 18


def test_put_feature_flag_sets_override_and_persists(client: TestClient) -> None:
    res = client.put("/v1/settings/CODE_EXECUTION", json={"value": "true"})
    assert res.status_code == 200

    code_execution = next(
        f for f in res.json()["features"] if f["key"] == "CODE_EXECUTION"
    )
    assert code_execution["effective_enabled"] is True
    assert code_execution["source"] == "override"
    assert code_execution["override"] == "true"

    # Persisted across a fresh GET, and actually flips the real gate function.
    reloaded = client.get("/v1/settings").json()
    reloaded_flag = next(
        f for f in reloaded["features"] if f["key"] == "CODE_EXECUTION"
    )
    assert reloaded_flag["effective_enabled"] is True

    from app.orchestrator import _code_execution_enabled

    assert _code_execution_enabled() is True


def test_put_feature_flag_empty_value_clears_override(client: TestClient) -> None:
    client.put("/v1/settings/WEB_SEARCH", json={"value": "true"})
    res = client.put("/v1/settings/WEB_SEARCH", json={"value": ""})
    web_search = next(f for f in res.json()["features"] if f["key"] == "WEB_SEARCH")
    assert web_search["source"] != "override"
    assert web_search["override"] is None


def test_put_feature_flag_rejects_malformed_value(client: TestClient) -> None:
    res = client.put("/v1/settings/CODE_EXECUTION", json={"value": "banana"})
    assert res.status_code == 400


def test_delete_clears_feature_flag_override(client: TestClient) -> None:
    client.put("/v1/settings/IMAGE_GENERATION", json={"value": "true"})
    res = client.delete("/v1/settings/IMAGE_GENERATION")
    assert res.status_code == 200
    image_gen = next(
        f for f in res.json()["features"] if f["key"] == "IMAGE_GENERATION"
    )
    assert image_gen["override"] is None


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
    client.put("/v1/settings/CODE_EXECUTION", json={"value": "true"})
    res = client.post("/v1/settings/reset")
    assert res.status_code == 200
    assert all(t["override"] is None for t in res.json()["tiers"])
    assert all(c["override"] is None for c in res.json()["categories"])
    assert all(f["override"] is None for f in res.json()["features"])


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


# --- Admin gate: open registration + JWT can't let any self-registered ------
# --- user rewrite global settings --------------------------------------------


def _register_login(
    client: TestClient, username: str, password: str = "password123"
) -> str:
    client.post("/v1/auth/register", json={"username": username, "password": password})
    resp = client.post(
        "/v1/auth/login", json={"username": username, "password": password}
    )
    return str(resp.json()["access_token"])


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_self_registered_user_blocked_from_settings_when_registration_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "settings-admin-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_USERNAMES", raising=False)
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")

    token = _register_login(client, "anyone")

    res = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(token)
    )
    assert res.status_code == 403
    assert "admin" in res.json()["detail"].lower()
    assert (
        client.delete("/v1/settings/MODEL_CODING", headers=_hdr(token)).status_code
        == 403
    )
    assert client.post("/v1/settings/reset", headers=_hdr(token)).status_code == 403


def test_admin_username_allowed_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "settings-admin-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_USERNAMES", "root, Admin")
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")

    admin_token = _register_login(client, "Admin")
    other_token = _register_login(client, "someone-else")

    ok = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(admin_token)
    )
    assert ok.status_code == 200

    blocked = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "y"}, headers=_hdr(other_token)
    )
    assert blocked.status_code == 403


def test_settings_writable_when_registration_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Closed registration implies an operator-provisioned, trusted user set —
    # today's any-authenticated-user behavior is preserved here.
    monkeypatch.setenv("JWT_SECRET", "settings-admin-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_USERNAMES", raising=False)
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    # Register while registration is briefly open, then close it, matching a
    # realistic operator flow (provision users, then lock the door).
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    token = _register_login(client, "provisioned-user")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    res = client.put(
        "/v1/settings/MODEL_CODING", json={"value": "x"}, headers=_hdr(token)
    )
    assert res.status_code == 200


def test_settings_writable_without_jwt(client: TestClient) -> None:
    # No JWT auth configured at all (static-token or auth-disabled mode):
    # the admin gate never applies — nothing to check identity against.
    res = client.put("/v1/settings/MODEL_CODING", json={"value": "x"})
    assert res.status_code == 200


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
