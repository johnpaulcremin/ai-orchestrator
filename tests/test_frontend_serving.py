from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fixture_dist(tmp_path: Path) -> Path:
    """A minimal built-frontend fixture: index.html plus one hashed asset."""
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>fixture app shell</body></html>",
        encoding="utf-8",
    )
    (assets / "index-abc123.js").write_text("console.log('fixture');", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    return dist


def test_root_is_json_when_dist_absent(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path / "no-such-dist"))
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ai-orchestrator"}


def test_unknown_path_404s_when_dist_absent(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path / "no-such-dist"))
    response = client.get("/some/client/route")
    assert response.status_code == 404


def test_root_serves_index_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "fixture app shell" in response.text


def test_static_asset_served_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "fixture" in response.text

    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.text == "{}"


def test_spa_fallback_for_unknown_client_route(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/some/client/route")
    assert response.status_code == 200
    assert "fixture app shell" in response.text


def test_api_routes_unaffected_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/v1/status")
    assert status.status_code == 200
    assert status.json()["service"] == "ai-orchestrator"
    assert client.get("/docs").status_code == 200
