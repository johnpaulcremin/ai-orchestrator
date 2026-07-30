"""Searching conversations by title or message content (GET /v1/search)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app.schemas import AskRequest, AskResponse

JWT_SECRET = "search-secret"


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


def _create(
    client: TestClient, title: str, headers: dict[str, str] | None = None
) -> int:
    return int(
        client.post("/v1/conversations", json={"title": title}, headers=headers).json()[
            "id"
        ]
    )


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        library_sources: list[dict] | None = None,
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def test_search_matches_conversation_title(client: TestClient) -> None:
    _create(client, "Trip to Japan")
    _create(client, "Grocery list")

    res = client.get("/v1/search", params={"q": "japan"})
    assert res.status_code == 200
    titles = [r["title"] for r in res.json()]
    assert titles == ["Trip to Japan"]
    assert res.json()[0]["snippet"] == "Trip to Japan"


def test_search_matches_message_content(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Untitled conversation")
    client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "tell me about volcanoes"}
    )

    res = client.get("/v1/search", params={"q": "volcanoes"})
    ids = [r["id"] for r in res.json()]
    assert cid in ids
    assert "volcanoes" in res.json()[0]["snippet"]


def test_search_is_case_insensitive(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client, "Untitled conversation")
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "The Eiffel Tower"})

    res = client.get("/v1/search", params={"q": "eiffel"})
    assert [r["id"] for r in res.json()] == [cid]


def test_search_no_match_returns_empty_list(client: TestClient) -> None:
    _create(client, "Trip to Japan")
    res = client.get("/v1/search", params={"q": "nonexistent term xyz"})
    assert res.json() == []


def test_search_requires_nonempty_query(client: TestClient) -> None:
    res = client.get("/v1/search", params={"q": ""})
    assert res.status_code == 422


def test_search_escapes_like_wildcards(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    _create(client, "50% off sale")
    _create(client, "5X0Y off sale")  # would falsely match "%" -> "_" wildcard leakage

    res = client.get("/v1/search", params={"q": "50%"})
    titles = [r["title"] for r in res.json()]
    assert titles == ["50% off sale"]


def test_search_is_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    _create(client, "Alice's secret trip", headers=_hdr(alice))
    _create(client, "Bob's secret trip", headers=_hdr(bob))

    alice_results = client.get(
        "/v1/search", params={"q": "secret"}, headers=_hdr(alice)
    ).json()
    bob_results = client.get(
        "/v1/search", params={"q": "secret"}, headers=_hdr(bob)
    ).json()

    assert [r["title"] for r in alice_results] == ["Alice's secret trip"]
    assert [r["title"] for r in bob_results] == ["Bob's secret trip"]


def test_search_orders_most_recently_updated_first(
    client: TestClient, db_path: Path
) -> None:
    first = _create(client, "match one")
    second = _create(client, "match two")
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE conversations SET updated_at = '2000-01-01 00:00:00'")
    client.patch(f"/v1/conversations/{first}", json={"title": "match one renamed"})

    res = client.get("/v1/search", params={"q": "match"})
    ids = [r["id"] for r in res.json()]
    assert ids == [first, second]
