"""Saved prompt templates CRUD (GET/POST/PATCH/DELETE /v1/templates)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

JWT_SECRET = "templates-secret"


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


def test_no_templates_returns_empty_list(client: TestClient) -> None:
    res = client.get("/v1/templates")
    assert res.status_code == 200
    assert res.json() == []


def test_create_then_list_template(client: TestClient) -> None:
    res = client.post(
        "/v1/templates", json={"name": "Summarize", "content": "Summarize this: "}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Summarize"
    assert body["content"] == "Summarize this: "
    assert "id" in body

    res = client.get("/v1/templates")
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["name"] == "Summarize"


def test_create_rejects_empty_name(client: TestClient) -> None:
    res = client.post("/v1/templates", json={"name": "", "content": "x"})
    assert res.status_code == 422


def test_create_rejects_empty_content(client: TestClient) -> None:
    res = client.post("/v1/templates", json={"name": "x", "content": ""})
    assert res.status_code == 422


def test_create_rejects_oversized_name(client: TestClient) -> None:
    res = client.post("/v1/templates", json={"name": "x" * 81, "content": "y"})
    assert res.status_code == 422


def test_update_name_only(client: TestClient) -> None:
    created = client.post(
        "/v1/templates", json={"name": "Old name", "content": "content"}
    ).json()
    res = client.patch(f"/v1/templates/{created['id']}", json={"name": "New name"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "New name"
    assert body["content"] == "content"


def test_update_content_only(client: TestClient) -> None:
    created = client.post(
        "/v1/templates", json={"name": "Name", "content": "old content"}
    ).json()
    res = client.patch(
        f"/v1/templates/{created['id']}", json={"content": "new content"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Name"
    assert body["content"] == "new content"


def test_update_with_neither_field_is_rejected(client: TestClient) -> None:
    created = client.post(
        "/v1/templates", json={"name": "Name", "content": "content"}
    ).json()
    res = client.patch(f"/v1/templates/{created['id']}", json={})
    assert res.status_code == 400


def test_update_missing_template_404(client: TestClient) -> None:
    res = client.patch("/v1/templates/999999", json={"name": "x"})
    assert res.status_code == 404


def test_delete_template(client: TestClient) -> None:
    created = client.post(
        "/v1/templates", json={"name": "Name", "content": "content"}
    ).json()
    res = client.delete(f"/v1/templates/{created['id']}")
    assert res.status_code == 200

    res = client.get("/v1/templates")
    assert res.json() == []


def test_delete_missing_template_404(client: TestClient) -> None:
    res = client.delete("/v1/templates/999999")
    assert res.status_code == 404


def test_templates_are_scoped_to_owner(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _enable_jwt(monkeypatch)
    alice = _register_login(client, "alice")
    bob = _register_login(client, "bob")

    created = client.post(
        "/v1/templates",
        json={"name": "Alice's template", "content": "content"},
        headers=_hdr(alice),
    ).json()

    bob_list = client.get("/v1/templates", headers=_hdr(bob)).json()
    alice_list = client.get("/v1/templates", headers=_hdr(alice)).json()
    assert bob_list == []
    assert [t["name"] for t in alice_list] == ["Alice's template"]

    # Bob can't update or delete Alice's template.
    res = client.patch(
        f"/v1/templates/{created['id']}", json={"name": "hijacked"}, headers=_hdr(bob)
    )
    assert res.status_code == 404
    res = client.delete(f"/v1/templates/{created['id']}", headers=_hdr(bob))
    assert res.status_code == 404
