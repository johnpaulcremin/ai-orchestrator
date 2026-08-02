"""Per-owner usage/spend summary (GET /v1/usage) built on the spend_log data
layer that already backs the daily budget cap (see app/budget.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database

JWT_SECRET = "usage-secret"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


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


def test_usage_defaults_to_zero_when_no_spend(client: TestClient) -> None:
    res = client.get("/v1/usage")
    assert res.status_code == 200
    body = res.json()
    assert body["today_usd"] == 0.0
    assert body["days"] == 14
    assert body["by_model"] == []
    assert len(body["by_day"]) == 14
    assert all(day["cost_usd"] == 0.0 for day in body["by_day"])
    assert all(day["tokens"] == 0 for day in body["by_day"])
    assert body["avoided_cost_today_usd"] == 0.0
    assert body["window_tokens"] == 0
    assert body["tokens_per_dollar"] is None


def test_usage_reports_todays_avoided_cost(client: TestClient, db_path: Path) -> None:
    database.record_avoided_cost(None, "gpt-5", "response_cache_hit", 0.01)
    database.record_avoided_cost(None, "gpt-5-mini", "response_cache_hit", 0.002)
    # A NULL avoided cost (unpriced original call) must not break the total.
    database.record_avoided_cost(None, "unpriced", "response_cache_hit", None)

    res = client.get("/v1/usage")
    assert res.json()["avoided_cost_today_usd"] == pytest.approx(0.012)


def test_usage_avoided_cost_is_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    database.record_avoided_cost("alice", "gpt-5", "response_cache_hit", 0.05)

    alice_usage = client.get("/v1/usage", headers=_hdr(alice)).json()
    bob_usage = client.get("/v1/usage", headers=_hdr(bob)).json()
    assert alice_usage["avoided_cost_today_usd"] == pytest.approx(0.05)
    assert bob_usage["avoided_cost_today_usd"] == 0.0


def test_usage_reports_todays_total(client: TestClient, db_path: Path) -> None:
    database.record_spend(None, "gpt-5", 100, 200, 0.01)
    database.record_spend(None, "gpt-5-mini", 50, 50, 0.002)
    # A NULL cost (unpriced model) must not break the total.
    database.record_spend(None, "unpriced", 10, 10, None)

    res = client.get("/v1/usage")
    assert res.json()["today_usd"] == pytest.approx(0.012)


def test_usage_breaks_down_by_model_sorted_by_cost(
    client: TestClient, db_path: Path
) -> None:
    database.record_spend(None, "gpt-5-mini", 50, 50, 0.002)
    database.record_spend(None, "gpt-5", 100, 200, 0.01)
    database.record_spend(None, "gpt-5", 100, 200, 0.01)

    res = client.get("/v1/usage")
    by_model = res.json()["by_model"]
    assert [row["model"] for row in by_model] == ["gpt-5", "gpt-5-mini"]

    gpt5 = by_model[0]
    assert gpt5["calls"] == 2
    assert gpt5["input_tokens"] == 200
    assert gpt5["output_tokens"] == 400
    assert gpt5["cost_usd"] == pytest.approx(0.02)


def test_usage_by_model_reports_unpriced_model_as_null_not_zero(
    client: TestClient, db_path: Path
) -> None:
    # A fully-unpriced model (every call recorded with cost_usd=None) must be
    # distinguishable from a genuinely free model (e.g. local Ollama, which
    # records 0.0) — reporting 0.0 for both would hide the fact that this
    # model's spend isn't actually being tracked or bounded.
    database.record_spend(None, "some-custom-model", 10, 10, None)
    database.record_spend(None, "some-custom-model", 10, 10, None)
    database.record_spend(None, "ollama/llama3.1:8b", 10, 10, 0.0)

    res = client.get("/v1/usage")
    by_model = {row["model"]: row for row in res.json()["by_model"]}

    assert by_model["some-custom-model"]["calls"] == 2
    assert by_model["some-custom-model"]["cost_usd"] is None
    assert by_model["ollama/llama3.1:8b"]["cost_usd"] == pytest.approx(0.0)


def test_usage_by_day_series_includes_backdated_spend(
    client: TestClient, db_path: Path
) -> None:
    database.record_spend(None, "gpt-5", 100, 100, 0.5)  # today
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE spend_log SET created_at = date('now', '-1 day') WHERE model = 'gpt-5'"
        )
        conn.execute(
            """
            INSERT INTO spend_log (owner, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (NULL, 'gpt-5', 10, 10, 0.25, date('now', '-10 days'))
            """
        )

    res = client.get("/v1/usage", params={"days": 3})
    by_day = res.json()["by_day"]
    assert len(by_day) == 3
    # -10 days falls outside the 3-day window: total spend is only the
    # backdated-to-yesterday 0.5, not 0.75.
    assert sum(day["cost_usd"] for day in by_day) == pytest.approx(0.5)
    assert by_day[-1]["cost_usd"] == pytest.approx(0.0)  # today: nothing recorded today
    assert by_day[-2]["cost_usd"] == pytest.approx(0.5)  # yesterday


# --- the real KPI: tokens_per_dollar / window_tokens ------------------------


def test_usage_reports_tokens_per_dollar_over_the_window(
    client: TestClient, db_path: Path
) -> None:
    database.record_spend(None, "gpt-5", 1000, 4000, 0.05)
    database.record_spend(None, "gpt-5-mini", 2000, 3000, 0.01)

    body = client.get("/v1/usage").json()
    # 1000+4000+2000+3000 = 10000 tokens over 0.06 total spend.
    assert body["window_tokens"] == 10000
    assert body["tokens_per_dollar"] == pytest.approx(10000 / 0.06)


def test_usage_tokens_per_dollar_is_none_when_window_has_zero_spend(
    client: TestClient, db_path: Path
) -> None:
    # A genuinely free model (e.g. local Ollama) still processes real tokens,
    # but a ratio against zero cost isn't a number — window_tokens is what
    # distinguishes "all free" from "no usage at all" on the frontend.
    database.record_spend(None, "ollama/llama3.1:8b", 500, 500, 0.0)

    body = client.get("/v1/usage").json()
    assert body["window_tokens"] == 1000
    assert body["tokens_per_dollar"] is None


def test_usage_tokens_per_dollar_ignores_unpriced_calls_cost_but_counts_their_tokens(
    client: TestClient, db_path: Path
) -> None:
    # An unpriced model's NULL cost must not break the ratio (NULL sums as 0
    # in SQL), while its tokens still count toward window_tokens.
    database.record_spend(None, "gpt-5", 1000, 1000, 0.02)
    database.record_spend(None, "some-custom-model", 500, 500, None)

    body = client.get("/v1/usage").json()
    assert body["window_tokens"] == 3000
    assert body["tokens_per_dollar"] == pytest.approx(3000 / 0.02)


def test_usage_by_day_reports_tokens_alongside_cost(
    client: TestClient, db_path: Path
) -> None:
    database.record_spend(None, "gpt-5", 1000, 500, 0.05)
    res = client.get("/v1/usage", params={"days": 1})
    day = res.json()["by_day"][0]
    assert day["tokens"] == 1500
    assert day["cost_usd"] == pytest.approx(0.05)


def test_usage_by_day_tokens_respects_the_backdated_window(
    client: TestClient, db_path: Path
) -> None:
    database.record_spend(None, "gpt-5", 100, 100, 0.5)  # today
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO spend_log (owner, model, input_tokens, output_tokens, cost_usd, created_at)
            VALUES (NULL, 'gpt-5', 900, 900, 5.0, date('now', '-10 days'))
            """
        )

    res = client.get("/v1/usage", params={"days": 3})
    body = res.json()
    # The -10-days row falls outside the 3-day window entirely.
    assert body["window_tokens"] == 200
    assert body["tokens_per_dollar"] == pytest.approx(200 / 0.5)


def test_usage_tokens_per_dollar_is_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    database.record_spend("alice", "gpt-5", 1000, 1000, 0.1)
    database.record_spend("bob", "gpt-5", 100, 100, 0.5)

    alice_usage = client.get("/v1/usage", headers=_hdr(alice)).json()
    bob_usage = client.get("/v1/usage", headers=_hdr(bob)).json()

    assert alice_usage["window_tokens"] == 2000
    assert alice_usage["tokens_per_dollar"] == pytest.approx(2000 / 0.1)
    assert bob_usage["window_tokens"] == 200
    assert bob_usage["tokens_per_dollar"] == pytest.approx(200 / 0.5)


def test_usage_is_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    database.record_spend("alice", "gpt-5", 100, 100, 0.03)
    database.record_spend("bob", "gpt-5", 100, 100, 0.07)

    alice_usage = client.get("/v1/usage", headers=_hdr(alice)).json()
    bob_usage = client.get("/v1/usage", headers=_hdr(bob)).json()

    assert alice_usage["today_usd"] == pytest.approx(0.03)
    assert bob_usage["today_usd"] == pytest.approx(0.07)


def test_usage_days_param_validation(client: TestClient) -> None:
    assert client.get("/v1/usage", params={"days": 0}).status_code == 422
    assert client.get("/v1/usage", params={"days": 91}).status_code == 422
    assert client.get("/v1/usage", params={"days": 90}).status_code == 200
    assert client.get("/v1/usage", params={"days": 1}).status_code == 200


def test_usage_days_1_reports_only_today(client: TestClient, db_path: Path) -> None:
    database.record_spend(None, "gpt-5", 100, 100, 0.5)
    res = client.get("/v1/usage", params={"days": 1})
    body = res.json()
    assert body["days"] == 1
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["cost_usd"] == pytest.approx(0.5)


# --- budget fields (T4.8-adjacent: the "future authenticated endpoint" -----
# budget.py's own docstring flagged this as a later addition) ---------------


def test_usage_omits_budget_fields_when_no_caps_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    body = client.get("/v1/usage").json()
    assert body["daily_budget_usd"] is None
    assert body["daily_budget_per_owner_usd"] is None
    assert body["owner_remaining_usd"] is None


def test_usage_surfaces_the_configured_global_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_BUDGET_USD", "10")
    monkeypatch.delenv("DAILY_BUDGET_PER_OWNER_USD", raising=False)
    body = client.get("/v1/usage").json()
    assert body["daily_budget_usd"] == pytest.approx(10.0)
    # No per-owner cap configured -> no remaining figure to report.
    assert body["daily_budget_per_owner_usd"] is None
    assert body["owner_remaining_usd"] is None


def test_usage_reports_owner_remaining_under_the_per_owner_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "1.0")
    database.record_spend(None, "gpt-5", 100, 100, 0.35)

    body = client.get("/v1/usage").json()
    assert body["daily_budget_per_owner_usd"] == pytest.approx(1.0)
    assert body["owner_remaining_usd"] == pytest.approx(0.65)


def test_usage_owner_remaining_floors_at_zero_not_negative(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "0.5")
    # Spend already past the cap (reachable via fallback overshoot).
    database.record_spend(None, "gpt-5", 100, 100, 0.9)

    body = client.get("/v1/usage").json()
    assert body["owner_remaining_usd"] == 0.0


def test_usage_owner_remaining_never_leaks_a_different_owners_spend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    monkeypatch.setenv("DAILY_BUDGET_PER_OWNER_USD", "1.0")
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    database.record_spend("alice", "gpt-5", 100, 100, 0.8)

    alice_usage = client.get("/v1/usage", headers=_hdr(alice)).json()
    bob_usage = client.get("/v1/usage", headers=_hdr(bob)).json()

    assert alice_usage["owner_remaining_usd"] == pytest.approx(0.2)
    # Bob's own spend is $0 — his remaining room must reflect that, not
    # alice's, even though both share the same configured per-owner cap.
    assert bob_usage["owner_remaining_usd"] == pytest.approx(1.0)


def test_usage_never_exposes_the_live_global_spend_total(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the configured LIMIT is exposed, never the actual global total —
    # that stays private to the operator (see budget.py's own boundary for
    # the anonymous /v1/status endpoint; this authenticated one keeps it too).
    monkeypatch.setenv("DAILY_BUDGET_USD", "10")
    database.record_spend(None, "gpt-5", 100, 100, 3.0)
    body = client.get("/v1/usage").json()
    assert "spent_today_usd" not in body
    assert "global_spent_usd" not in body
    assert body["daily_budget_usd"] == pytest.approx(10.0)


# --- Retention: GET /v1/usage stays accurate across the prune boundary ------
# (see app/retention.py — spend_log detail rolled into spend_rollup before
# being pruned; usage_summary's by_model/by_day are unioned with it)


def test_usage_by_model_reflects_pruned_history_via_rollup(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import retention

    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    database.record_spend(None, "gpt-5", 100, 100, 1.0)
    # Old enough to age past the 30-day retention window, but still well
    # inside the 90-day query window below — otherwise the rollup row
    # itself would be outside window_start_month and correctly excluded.
    old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with sqlite3.connect(db_path) as conn:
        row_id = conn.execute("SELECT MAX(id) FROM spend_log").fetchone()[0]
        conn.execute("UPDATE spend_log SET created_at = ? WHERE id = ?", (old, row_id))
    database.record_spend(None, "gpt-5", 100, 100, 2.0)  # stays in detail

    pruned = retention.rollup_and_prune()
    assert pruned["spend_log"] == 1

    body = client.get("/v1/usage", params={"days": 90}).json()
    gpt5 = next(m for m in body["by_model"] if m["model"] == "gpt-5")
    # Both the pruned (rolled-up) call and the still-live detail call count.
    assert gpt5["calls"] == 2
    assert gpt5["cost_usd"] == pytest.approx(3.0)


def test_usage_by_model_unaffected_when_nothing_has_been_pruned(
    client: TestClient, db_path: Path
) -> None:
    """Default retention (365 days) — a fresh test DB's rows are never old
    enough to prune, so by_model must match plain, unrolled-up detail."""
    database.record_spend(None, "gpt-5", 100, 100, 1.0)
    database.record_spend(None, "gpt-5", 100, 100, 2.0)

    body = client.get("/v1/usage", params={"days": 14}).json()

    gpt5 = next(m for m in body["by_model"] if m["model"] == "gpt-5")
    assert gpt5["calls"] == 2
    assert gpt5["cost_usd"] == pytest.approx(3.0)
