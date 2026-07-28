"""Self-updating model/pricing catalog (app/model_catalog.py): pulls
LiteLLM's published pricing feed instead of relying only on the hand-
maintained defaults in app/usage.py. Opt-in (MODEL_CATALOG_SYNC=false by
default) since this is the only thing in this app that calls a server
other than a configured LLM provider.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import database, model_catalog, usage


@pytest.fixture()
def catalog_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_CATALOG_SYNC", "true")


def _fake_feed_response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/feed.json")
    return httpx.Response(200, json=payload, request=request)


_SAMPLE_FEED = {
    "gpt-4o": {
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
        "litellm_provider": "openai",
        "mode": "chat",
    },
    "claude-3-5-sonnet-20241022": {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "litellm_provider": "anthropic",
        "mode": "chat",
    },
    "text-embedding-3-small": {
        # No per-token input/output cost — priced differently; must be skipped.
        "input_cost_per_token": 0.00000002,
        "litellm_provider": "openai",
        "mode": "embedding",
    },
}


# --- config / flags ------------------------------------------------------------


def test_enabled_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_CATALOG_SYNC", raising=False)
    assert model_catalog.enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_enabled_can_be_turned_on(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_CATALOG_SYNC", value)
    assert model_catalog.enabled() is True


def test_sync_interval_hours_default_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_CATALOG_SYNC_INTERVAL_HOURS", raising=False)
    assert model_catalog.sync_interval_hours() == 24
    monkeypatch.setenv("MODEL_CATALOG_SYNC_INTERVAL_HOURS", "not-a-number")
    assert model_catalog.sync_interval_hours() == 24
    monkeypatch.setenv("MODEL_CATALOG_SYNC_INTERVAL_HOURS", "6")
    assert model_catalog.sync_interval_hours() == 6


# --- _parse_feed -----------------------------------------------------------------


def test_parse_feed_extracts_chat_models_and_converts_to_per_million() -> None:
    pricing = model_catalog._parse_feed(_SAMPLE_FEED)
    assert pricing["gpt-4o"] == pytest.approx((2.5, 10.0))
    assert pricing["claude-3-5-sonnet-20241022"] == pytest.approx((3.0, 15.0))


def test_parse_feed_skips_entries_without_both_token_costs() -> None:
    pricing = model_catalog._parse_feed(_SAMPLE_FEED)
    assert "text-embedding-3-small" not in pricing


def test_parse_feed_skips_non_dict_entries() -> None:
    pricing = model_catalog._parse_feed(
        {"weird": "not-a-dict", "gpt-4o": _SAMPLE_FEED["gpt-4o"]}
    )
    assert list(pricing.keys()) == ["gpt-4o"]


# --- sync_now: gating and fail-safety --------------------------------------------


def test_sync_now_is_noop_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODEL_CATALOG_SYNC", raising=False)
    called = []
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: called.append(1) or None
    )
    result = model_catalog.sync_now()
    assert called == []
    assert result["enabled"] is False


def test_sync_now_stores_the_parsed_catalog(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    result = model_catalog.sync_now()
    assert result["enabled"] is True
    assert result["model_count"] == 2
    assert result["synced_at"] is not None
    assert result["stale"] is False
    assert "error" not in result or result.get("error") is None


def test_sync_now_first_sync_reports_no_new_models(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to diff against on the very first sync — reporting the whole
    feed as "new" would be noise, not a useful notice."""
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    result = model_catalog.sync_now()
    assert result["new_models"] == []


def test_sync_now_reports_models_added_since_last_sync(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()

    expanded = dict(_SAMPLE_FEED)
    expanded["gpt-6"] = {
        "input_cost_per_token": 0.000005,
        "output_cost_per_token": 0.00002,
        "mode": "chat",
    }
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(expanded)
    )
    result = model_catalog.sync_now()
    assert result["new_models"] == ["gpt-6"]
    assert result["model_count"] == 3


def test_sync_now_network_failure_keeps_the_previous_catalog(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    first = model_catalog.sync_now()
    assert first["model_count"] == 2

    def boom(*a, **k):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://x"))

    monkeypatch.setattr(model_catalog.httpx, "get", boom)
    second = model_catalog.sync_now()
    assert second["error"] is not None
    assert second["model_count"] == 2  # unchanged, not wiped


def test_sync_now_non_200_response_keeps_the_previous_catalog(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()

    def bad_status(*a, **k):
        request = httpx.Request("GET", "https://x")
        return httpx.Response(500, request=request)

    monkeypatch.setattr(model_catalog.httpx, "get", bad_status)
    result = model_catalog.sync_now()
    assert result["error"] is not None
    assert result["model_count"] == 2


def test_sync_now_malformed_json_keeps_the_previous_catalog(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()

    def not_a_dict(*a, **k):
        request = httpx.Request("GET", "https://x")
        return httpx.Response(200, json=["not", "a", "dict"], request=request)

    monkeypatch.setattr(model_catalog.httpx, "get", not_a_dict)
    result = model_catalog.sync_now()
    assert result["error"] is not None
    assert result["model_count"] == 2


# --- sync_if_stale / status --------------------------------------------------------


def test_status_reports_enabled_but_never_synced(
    db_path: Path, catalog_on: None
) -> None:
    result = model_catalog.status()
    assert result["enabled"] is True
    assert result["synced_at"] is None
    assert result["model_count"] == 0
    assert result["stale"] is True


def test_status_is_never_stale_when_disabled(db_path: Path) -> None:
    result = model_catalog.status()
    assert result["enabled"] is False
    assert result["stale"] is False


def test_sync_if_stale_triggers_a_fetch_when_never_synced(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        model_catalog.httpx,
        "get",
        lambda *a, **k: calls.append(1) or _fake_feed_response(_SAMPLE_FEED),
    )
    result = model_catalog.sync_if_stale()
    assert len(calls) == 1
    assert result["model_count"] == 2
    assert result["stale"] is False


def test_sync_if_stale_does_not_fetch_when_fresh(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        model_catalog.httpx,
        "get",
        lambda *a, **k: calls.append(1) or _fake_feed_response(_SAMPLE_FEED),
    )
    model_catalog.sync_now()
    assert len(calls) == 1

    result = model_catalog.sync_if_stale()
    assert len(calls) == 1  # no second fetch — the cached catalog is fresh
    assert result["model_count"] == 2


def test_sync_if_stale_refetches_past_the_interval(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_CATALOG_SYNC_INTERVAL_HOURS", "1")
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE model_catalog SET fetched_at = datetime('now', '-2 hours')"
        )

    calls = []
    monkeypatch.setattr(
        model_catalog.httpx,
        "get",
        lambda *a, **k: calls.append(1) or _fake_feed_response(_SAMPLE_FEED),
    )
    model_catalog.sync_if_stale()
    assert len(calls) == 1


def test_sync_if_stale_is_noop_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: calls.append(1) or None
    )
    result = model_catalog.sync_if_stale()
    assert calls == []
    assert result["enabled"] is False


# --- cached_pricing / usage.py integration -----------------------------------------


def test_cached_pricing_empty_when_disabled(db_path: Path) -> None:
    assert model_catalog.cached_pricing() == {}


def test_cached_pricing_empty_when_never_synced(
    db_path: Path, catalog_on: None
) -> None:
    assert model_catalog.cached_pricing() == {}


def test_cached_pricing_returns_the_synced_table(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()
    pricing = model_catalog.cached_pricing()
    assert pricing["gpt-4o"] == pytest.approx((2.5, 10.0))


def test_usage_estimate_cost_uses_the_synced_catalog_for_an_unlisted_model(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model with no hand-bundled default (not in usage._DEFAULT_PRICING)
    must still price correctly once the live catalog has it."""
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()

    cost = usage.estimate_cost(
        "gpt-4o", usage.Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(2.5 + 10.0)


def test_usage_default_pricing_wins_over_the_catalog(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-bundled defaults are deliberately curated for this app's own
    tiers — the auto-fetched catalog must never silently override them."""
    feed_with_conflict = dict(_SAMPLE_FEED)
    feed_with_conflict["gpt-5"] = {
        "input_cost_per_token": 999.0,
        "output_cost_per_token": 999.0,
        "mode": "chat",
    }
    monkeypatch.setattr(
        model_catalog.httpx,
        "get",
        lambda *a, **k: _fake_feed_response(feed_with_conflict),
    )
    model_catalog.sync_now()

    cost = usage.estimate_cost(
        "gpt-5", usage.Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    # 1.25 (input) + 10.0 (output) from _DEFAULT_PRICING, not the absurd
    # 999+999 the catalog would have produced.
    assert cost == pytest.approx(1.25 + 10.0)


def test_usage_model_pricing_env_wins_over_everything(
    db_path: Path, catalog_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    model_catalog.sync_now()
    monkeypatch.setenv("MODEL_PRICING", json.dumps({"gpt-4o": [1.0, 1.0]}))

    cost = usage.estimate_cost(
        "gpt-4o", usage.Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == pytest.approx(1.0 + 1.0)


# --- HTTP endpoints -----------------------------------------------------------------


def test_model_catalog_status_endpoint_disabled_by_default(
    client: TestClient,
) -> None:
    body = client.get("/v1/model-catalog").json()
    assert body["enabled"] is False
    assert body["synced_at"] is None
    assert body["stale"] is False


def test_model_catalog_status_endpoint_triggers_a_sync_when_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_CATALOG_SYNC", "true")
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    body = client.get("/v1/model-catalog").json()
    assert body["model_count"] == 2
    assert body["synced_at"] is not None


def test_model_catalog_sync_endpoint_forces_a_sync(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_CATALOG_SYNC", "true")
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: _fake_feed_response(_SAMPLE_FEED)
    )
    body = client.post("/v1/model-catalog/sync").json()
    assert body["model_count"] == 2


def test_model_catalog_sync_endpoint_noop_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        model_catalog.httpx, "get", lambda *a, **k: calls.append(1) or None
    )
    body = client.post("/v1/model-catalog/sync").json()
    assert calls == []
    assert body["enabled"] is False


def test_model_catalog_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/model-catalog").status_code == 401
    assert client.post("/v1/model-catalog/sync").status_code == 401
    ok = client.get(
        "/v1/model-catalog", headers={"Authorization": "Bearer secret-token"}
    )
    assert ok.status_code == 200


def test_model_catalog_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "MODEL_CATALOG_SYNC")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False


# --- database layer ---------------------------------------------------------------


def test_get_model_catalog_returns_none_when_never_synced(db_path: Path) -> None:
    assert database.get_model_catalog() is None


def test_set_model_catalog_then_get_round_trips(db_path: Path) -> None:
    database.set_model_catalog(
        pricing_json=json.dumps({"gpt-4o": [2.5, 10.0]}),
        model_names_json=json.dumps(["gpt-4o"]),
        new_models_json=json.dumps([]),
        model_count=1,
    )
    row = database.get_model_catalog()
    assert row is not None
    assert json.loads(row["pricing_json"]) == {"gpt-4o": [2.5, 10.0]}
    assert row["model_count"] == 1


def test_set_model_catalog_upserts_the_singleton_row(db_path: Path) -> None:
    database.set_model_catalog(
        pricing_json=json.dumps({"a": [1.0, 1.0]}),
        model_names_json=json.dumps(["a"]),
        new_models_json=json.dumps([]),
        model_count=1,
    )
    database.set_model_catalog(
        pricing_json=json.dumps({"b": [2.0, 2.0]}),
        model_names_json=json.dumps(["b"]),
        new_models_json=json.dumps(["b"]),
        model_count=1,
    )
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_catalog").fetchone()[0]
    assert count == 1  # still a singleton, not a second row
    row = database.get_model_catalog()
    assert row is not None
    assert json.loads(row["pricing_json"]) == {"b": [2.0, 2.0]}
