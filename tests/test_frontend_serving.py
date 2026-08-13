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


def test_root_is_json_when_dist_absent(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path / "no-such-dist"))
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ai-orchestrator"}
    assert response.headers["content-security-policy"] == "default-src 'none'"


def test_unknown_path_404s_when_dist_absent(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path / "no-such-dist"))
    response = client.get("/some/client/route")
    assert response.status_code == 404


_FRONTEND_CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; script-src 'self'; style-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'self'; "
    "frame-ancestors 'none'"
)


def test_root_serves_index_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "fixture app shell" in response.text
    # Regression: the API's default-src 'none' CSP blocks the frontend's own
    # scripts/styles from running, showing a blank page (see CHANGELOG).
    assert response.headers["content-security-policy"] == _FRONTEND_CSP


def test_static_asset_served_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert "fixture" in response.text
    assert response.headers["content-security-policy"] == _FRONTEND_CSP

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
    assert response.headers["content-security-policy"] == _FRONTEND_CSP


def test_api_routes_unaffected_when_dist_present(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    assert client.get("/health").json() == {"status": "ok"}
    status = client.get("/v1/status")
    assert status.status_code == 200
    assert status.json()["service"] == "ai-orchestrator"
    assert client.get("/docs").status_code == 200


def test_api_prefixed_requests_reach_the_same_routes(client: TestClient) -> None:
    """The frontend's fetch client calls `/api/v1/...` (frontend/src/App.tsx's
    `API_BASE`), expecting a proxy in front of this backend to strip that
    prefix — true of both the Vite dev proxy and frontend/nginx.conf, but not
    of this backend serving itself directly. Without the /api-stripping
    middleware, these would silently fall through to the SPA catch-all
    (a 200 of the wrong content for GET, a 405 for POST) instead of 404ing
    honestly or reaching the real route.
    """
    plain = client.get("/v1/status")
    prefixed = client.get("/api/v1/status")
    assert prefixed.status_code == plain.status_code == 200
    assert prefixed.json() == plain.json()

    # POST reaches the real route (401, unauthenticated) rather than 405ing
    # off the GET-only SPA catch-all.
    response = client.post("/api/v1/conversations", json={})
    assert response.status_code != 405


# --- cache policy ----------------------------------------------------------------
#
# Without an explicit Cache-Control header, browsers cache heuristically —
# and iOS Safari (worst as an installed PWA) served a stale index.html for
# hours after a rebuild, still referencing the OLD hashed bundle, so a
# freshly deployed frontend "didn't change" on the phone. Observed live
# through the tailscale tunnel. The split below is what makes deploys
# propagate on the next load while assets stay maximally cached.


def test_root_route_serves_index_with_no_cache(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'/' is an explicit route with its own dist logic (routers/system.py),
    not the SPA catch-all — the header must be asserted there separately or
    the entry point most phones actually load stays heuristically cached."""
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_index_html_must_revalidate(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    for path in ("/some/client/route",):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vite content-hashes every filename under /assets/, so a changed file
    is a NEW url — the old one may cache forever without ever going stale."""
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_non_asset_files_revalidate_like_the_entry_point(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """favicon/manifest keep mutable names — same rule as index.html."""
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    (fixture_dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_missing_asset_404s_instead_of_serving_the_shell(
    client: TestClient, fixture_dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing /assets/ file is a dead reference — most likely a stale
    cached shell asking for a bundle a rebuild replaced — never a client
    route. The SPA fallback used to hand it index.html: text/html where the
    browser expected CSS/JS, turning stale-cache into half-styled breakage
    instead of a clean 404. Found by probing an old bundle name through the
    live tunnel."""
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(fixture_dist))
    response = client.get("/assets/index-oldhash.css")
    assert response.status_code == 404
    # ...while genuine client routes still fall back to the shell.
    assert client.get("/some/client/route").status_code == 200
