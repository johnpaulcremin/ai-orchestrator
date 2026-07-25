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
