"""Baseline security response headers (app/security_headers.py) applied to
every response this backend sends.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_ordinary_route_gets_the_four_baseline_headers(client: TestClient) -> None:
    res = client.get("/v1/settings")
    assert res.headers["Content-Security-Policy"] == "default-src 'none'"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_headers_present_even_on_a_404(client: TestClient) -> None:
    res = client.get("/v1/conversations/999999/messages")
    assert res.status_code == 404
    assert res.headers["Content-Security-Policy"] == "default-src 'none'"
    assert res.headers["X-Frame-Options"] == "DENY"


def test_headers_present_even_on_a_422_validation_error(client: TestClient) -> None:
    res = client.post("/v1/conversations", json={"title": ""})
    assert res.status_code == 422
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_docs_route_is_exempt_from_csp_but_keeps_the_other_headers(
    client: TestClient,
) -> None:
    res = client.get("/docs")
    assert "Content-Security-Policy" not in res.headers
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert res.headers["X-Content-Type-Options"] == "nosniff"


def test_ordinary_route_has_no_x_robots_tag(client: TestClient) -> None:
    res = client.get("/v1/settings")
    assert "X-Robots-Tag" not in res.headers


# --- X-Robots-Tag on the public share route only ------------------------------


def test_share_route_gets_noindex_on_success(client: TestClient) -> None:
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    token = client.post(f"/v1/conversations/{cid}/share", json={}).json()["token"]

    res = client.get(f"/v1/shared/{token}")
    assert res.status_code == 200
    assert res.headers["X-Robots-Tag"] == "noindex"


def test_share_route_gets_noindex_even_on_a_404(client: TestClient) -> None:
    res = client.get("/v1/shared/no-such-token")
    assert res.status_code == 404
    assert res.headers["X-Robots-Tag"] == "noindex"
