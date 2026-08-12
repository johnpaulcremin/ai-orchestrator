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


# --- provenance on every answer (option 2) --------------------------------------


def test_every_ask_response_carries_the_deployment_id(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stamped by AskResponse's default_factory, so every construction site —
    including ones not written yet — carries it without remembering to."""
    import app.orchestrator as orchestrator
    from app.orchestrator import run_orchestrator
    from app.schemas import AskRequest, Mode

    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **kw: "An answer.")

    result = run_orchestrator(AskRequest(question="hello?", mode=Mode.fast))
    assert result.deployment_id == database.deployment_id()


def test_a_cached_answer_carries_the_CURRENT_deployment_id(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point is provenance of the RESPONSE, not the stored entry: a cache
    hit reconstructs a fresh AskResponse, so the id reflects the database
    serving the hit."""
    import app.orchestrator as orchestrator
    from app.orchestrator import run_orchestrator
    from app.schemas import AskRequest, Mode

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **kw: "An answer.")

    first = run_orchestrator(AskRequest(question="cache me", mode=Mode.fast))
    second = run_orchestrator(AskRequest(question="cache me", mode=Mode.fast))
    assert second.cached is True
    assert second.deployment_id == database.deployment_id()
    assert first.deployment_id == second.deployment_id


def test_stream_done_event_carries_the_deployment_id(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.orchestrator as orchestrator
    from app.orchestrator import stream_orchestrator
    from app.schemas import AskRequest, Mode

    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **kw: iter(["An answer."])
    )

    events = list(stream_orchestrator(AskRequest(question="hello?", mode=Mode.fast)))
    done = next(e for e in events if e["event"] == "done")
    assert done["data"]["deployment_id"] == database.deployment_id()


def test_uninitialized_database_yields_empty_id_never_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory rides on EVERY AskResponse — provenance metadata must
    never be the reason an answer fails. No schema -> "" (which the frontend
    guard skips), not memoized, so the real id appears as soon as init_db
    has run."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "no-schema.db"))
    assert database.deployment_id() == ""
    init_db()
    real = database.deployment_id()
    assert real != "" and len(real) == 16
