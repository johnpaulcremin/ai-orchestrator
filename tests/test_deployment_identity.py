"""Deployment identity (database.deployment_id, /v1/usage, /v1/status) — the
guard built after a scratch-DB verification backend silently co-bound the
API port (Windows SO_REUSEADDR permits it, no error) and its seeded figures
rendered in the real UI's header. The identity that matters is the DATABASE,
not the process: the dev server hot-reloads constantly, so a per-process id
would cry wolf, while "a different database's numbers are on screen" is
exactly the incident.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database
from app.database import init_db


def test_deployment_id_is_created_once_and_stable(db_path: Path) -> None:
    first = database.deployment_id()
    assert first and len(first) == 16
    assert database.deployment_id() == first  # re-read: same token


def test_deployment_id_survives_a_simulated_restart(db_path: Path) -> None:
    """The whole point of anchoring it in the database: a process restart
    (init_db running again over the same file) must not rotate it — only a
    DIFFERENT database may present a different identity."""
    first = database.deployment_id()
    init_db()  # what a restarted process does on startup
    assert database.deployment_id() == first


def test_different_databases_have_different_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = []
    for name in ("a.db", "b.db"):
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / name))
        init_db()
        ids.append(database.deployment_id())
    assert ids[0] != ids[1]


def test_identity_table_cannot_hold_a_second_row(db_path: Path) -> None:
    database.deployment_id()
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO deployment_identity (id, token) VALUES (2, 'x')")


def test_usage_response_carries_the_deployment_id(client: TestClient) -> None:
    body = client.get("/v1/usage").json()
    assert body["deployment_id"] == database.deployment_id()


def test_public_status_exposes_instance_id_but_never_deployment_id(
    client: TestClient,
) -> None:
    """The split is deliberate: instance_id rotates per process (useful for
    debugging, worthless for tracking), while the STABLE id on the
    unauthenticated status endpoint would let an anonymous caller
    fingerprint this deployment across restarts — so it lives only on the
    authed usage response."""
    from app.telemetry import INSTANCE_ID

    body = client.get("/v1/status").json()
    assert body["instance_id"] == INSTANCE_ID
    assert "deployment_id" not in body
