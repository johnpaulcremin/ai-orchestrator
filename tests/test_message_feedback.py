"""Message-level quality feedback (PUT /v1/conversations/{id}/messages/{id}/feedback,
GET /v1/feedback/summary) — see app/feedback.py and app/database.py's
messages.feedback/feedback_log for the full design rationale.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app import database
from app.schemas import AskRequest, AskResponse


def _stub_run_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    mode_used: str = "auto->fast",
    model: str | None = None,
) -> None:
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
        forced_category: str | None = None,
    ) -> AskResponse:
        return AskResponse(
            answer="canned answer", mode_used=mode_used, notes="n", model=model
        )

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def _ask(client: TestClient, cid: int, question: str = "hello") -> None:
    assert (
        client.post(
            f"/v1/conversations/{cid}/ask", json={"question": question}
        ).status_code
        == 200
    )


def _assistant_message_id(client: TestClient, cid: int) -> int:
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    return int(next(m for m in messages if m["role"] == "assistant")["id"])


def _user_message_id(client: TestClient, cid: int) -> int:
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    return int(next(m for m in messages if m["role"] == "user")["id"])


# --- schema: additive columns/table (not the numbered _MIGRATIONS system) --------
# messages.feedback/feedback_reason/model and feedback_log are simple additive
# adds (ALTER TABLE ADD COLUMN / CREATE TABLE IF NOT EXISTS), per this
# codebase's own convention doc in app/database.py -- deliberately not part of
# the numbered _MIGRATIONS system, which is reserved for renames/drops/
# backfills. These assert init_db() actually created them, on a fresh DB.


def _column_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def test_fresh_db_has_the_feedback_columns_on_messages(db_path) -> None:
    columns = _column_names(db_path, "messages")
    assert {"feedback", "feedback_reason", "model"} <= columns


def test_fresh_db_has_the_feedback_log_table_and_index(db_path) -> None:
    columns = _column_names(db_path, "feedback_log")
    assert columns == {
        "id",
        "owner",
        "message_id",
        "model",
        "mode_used",
        "category",
        "verdict",
        "reason",
        "created_at",
    }
    assert "idx_feedback_log_created_at" in _index_names(db_path, "feedback_log")


# --- toggle/clear semantics ------------------------------------------------------


def test_new_messages_default_to_no_feedback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert all(m["feedback"] is None for m in messages)


def test_rates_a_message_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    assert res.status_code == 200
    assert res.json()["feedback"] == 1


def test_rates_a_message_down_with_a_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down", "reason": "Incomplete"},
    )
    assert res.status_code == 200
    assert res.json()["feedback"] == -1
    assert res.json()["feedback_reason"] == "Incomplete"


def test_setting_the_same_verdict_again_clears_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    assert res.json()["feedback"] is None
    assert res.json()["feedback_reason"] is None


def test_switching_from_up_to_down_does_not_clear(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )
    assert res.json()["feedback"] == -1


def test_explicit_null_verdict_always_clears(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": None},
    )
    assert res.json()["feedback"] is None


# --- assistant-only --------------------------------------------------------------


def test_rating_a_user_message_is_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _user_message_id(client, cid)

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    assert res.status_code == 422


# --- pure marker ------------------------------------------------------------------


def test_rating_does_not_touch_conversation_updated_at(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    before_updated_at = client.get("/v1/conversations").json()[0]["updated_at"]

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )

    after_updated_at = client.get("/v1/conversations").json()[0]["updated_at"]
    assert after_updated_at == before_updated_at


# --- 404s / scoping ----------------------------------------------------------------


def test_rate_nonexistent_message_is_404(client: TestClient) -> None:
    cid = _create(client)
    res = client.put(
        f"/v1/conversations/{cid}/messages/999999/feedback",
        json={"verdict": "up"},
    )
    assert res.status_code == 404


def test_rate_404_for_missing_conversation(client: TestClient) -> None:
    res = client.put(
        "/v1/conversations/999999/messages/1/feedback",
        json={"verdict": "up"},
    )
    assert res.status_code == 404


def test_rate_scoped_to_its_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid_a = _create(client, "a")
    cid_b = _create(client, "b")
    _ask(client, cid_a)
    message_id = _assistant_message_id(client, cid_a)

    res = client.put(
        f"/v1/conversations/{cid_b}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    assert res.status_code == 404


def test_rate_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "feedback-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )
    bob = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "password123"}
    ).json()["access_token"]

    cid = client.post(
        "/v1/conversations",
        json={"title": "alice's chat"},
        headers={"Authorization": f"Bearer {alice}"},
    ).json()["id"]
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    message_id = _assistant_message_id_with_headers(
        client, cid, {"Authorization": f"Bearer {alice}"}
    )

    res = client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
        headers={"Authorization": f"Bearer {bob}"},
    )
    assert res.status_code == 404


def _assistant_message_id_with_headers(client, cid, headers) -> int:
    messages = client.get(f"/v1/conversations/{cid}/messages", headers=headers).json()
    return int(next(m for m in messages if m["role"] == "assistant")["id"])


# --- ledger: append on set/change/clear, correct snapshots -----------------------


def test_ledger_appends_a_row_with_the_model_and_category_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(
        monkeypatch, mode_used="auto->fast:coding", model="gpt-5-mini"
    )
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down", "reason": "Wrong"},
    )

    entries = database.feedback_log_entries(None, days=1)
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5-mini"
    assert entries[0]["category"] == "coding"
    assert entries[0]["mode_used"] == "auto->fast:coding"
    assert entries[0]["verdict"] == -1
    assert entries[0]["reason"] == "Wrong"


def test_ledger_gets_a_new_row_per_change_not_just_the_first_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )

    entries = database.feedback_log_entries(None, days=1)
    assert len(entries) == 2
    assert [e["verdict"] for e in entries] == [1, -1]


def test_ledger_survives_the_message_being_deleted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )
    client.delete(f"/v1/conversations/{cid}/messages/{message_id}")

    entries = database.feedback_log_entries(None, days=1)
    assert len(entries) == 1
    assert entries[0]["verdict"] == -1


def test_ledger_survives_a_regenerate_that_replaces_the_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )

    res = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert res.status_code == 200
    new_message_id = _assistant_message_id(client, cid)
    assert new_message_id != message_id
    # The new message is unrated (a fresh answer); the old rating still
    # lives in the ledger even though its message row is gone.
    assert (
        client.get(f"/v1/conversations/{cid}/messages").json()[-1]["feedback"] is None
    )

    entries = database.feedback_log_entries(None, days=1)
    assert len(entries) == 1
    assert entries[0]["verdict"] == -1


# --- summary math + lane derivation ----------------------------------------------


def test_summary_reports_per_model_stats(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch, mode_used="forced:claude-sonnet-5")
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )

    summary = client.get("/v1/feedback/summary").json()
    assert summary["by_model"]["claude-sonnet-5"] == {
        "answers_rated": 1,
        "up": 0,
        "down": 1,
        "down_rate": 1.0,
    }


def test_summary_derives_free_lane_from_mode_used(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(
        monkeypatch, mode_used="auto->free:groq/llama-3.3-70b-versatile"
    )
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )

    summary = client.get("/v1/feedback/summary").json()
    assert summary["by_lane"]["free"]["up"] == 1
    assert summary["by_lane"]["free"]["answers_rated"] == 1


def test_summary_derives_category_from_mode_used(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch, mode_used="auto->smart:reasoning")
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down"},
    )

    summary = client.get("/v1/feedback/summary").json()
    assert summary["by_category"]["reasoning"]["down"] == 1


def test_summary_excludes_the_clear_event_itself(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clear (verdict=0) contributes nothing to the aggregation, but the
    earlier SET event it cleared remains in the ledger's history — clearing
    is not retroactive erasure, same as the rest of feedback_log's
    append-only design."""
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},  # clears it
    )

    summary = client.get("/v1/feedback/summary").json()
    assert summary["by_lane"]["fast"] == {
        "answers_rated": 1,
        "up": 1,
        "down": 0,
        "down_rate": 0.0,
    }


def test_clear_actually_appends_a_verdict_zero_ledger_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The previous test (test_summary_excludes_the_clear_event_itself) only
    proves a clear is excluded from the AGGREGATED summary — that would pass
    identically whether the clear event is written and then filtered, or
    never written at all (feedback_log_entries filters verdict != 0). This
    asserts the row is genuinely INSERTed, not silently skipped."""
    import sqlite3

    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)

    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},  # clears it
    )

    conn = sqlite3.connect(database._db_path())
    rows = conn.execute(
        "SELECT verdict FROM feedback_log WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    conn.close()

    assert [r[0] for r in rows] == [1, 0]


def test_summary_respects_the_days_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )

    # Backdate the ledger row well outside a 1-day window.
    import sqlite3

    conn = sqlite3.connect(database._db_path())
    conn.execute("UPDATE feedback_log SET created_at = datetime('now', '-10 days')")
    conn.commit()
    conn.close()

    summary = client.get("/v1/feedback/summary?days=1").json()
    assert summary["by_lane"] == {}


def test_summary_scoped_by_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /v1/feedback/summary must never mix one owner's ratings into
    another's aggregate — the same privacy boundary test_rate_scoped_to_owner
    already covers for the PUT endpoint, here for the read side."""
    _stub_run_orchestrator(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "feedback-summary-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )
    bob = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "password123"}
    ).json()["access_token"]

    alice_headers = {"Authorization": f"Bearer {alice}"}
    bob_headers = {"Authorization": f"Bearer {bob}"}

    alice_cid = client.post(
        "/v1/conversations", json={"title": "alice's chat"}, headers=alice_headers
    ).json()["id"]
    client.post(
        f"/v1/conversations/{alice_cid}/ask",
        json={"question": "hi"},
        headers=alice_headers,
    )
    alice_message_id = _assistant_message_id_with_headers(
        client, alice_cid, alice_headers
    )
    client.put(
        f"/v1/conversations/{alice_cid}/messages/{alice_message_id}/feedback",
        json={"verdict": "down"},
        headers=alice_headers,
    )

    bob_cid = client.post(
        "/v1/conversations", json={"title": "bob's chat"}, headers=bob_headers
    ).json()["id"]
    client.post(
        f"/v1/conversations/{bob_cid}/ask", json={"question": "hi"}, headers=bob_headers
    )
    bob_message_id = _assistant_message_id_with_headers(client, bob_cid, bob_headers)
    client.put(
        f"/v1/conversations/{bob_cid}/messages/{bob_message_id}/feedback",
        json={"verdict": "up"},
        headers=bob_headers,
    )

    alice_summary = client.get("/v1/feedback/summary", headers=alice_headers).json()
    bob_summary = client.get("/v1/feedback/summary", headers=bob_headers).json()

    assert alice_summary["by_lane"]["fast"] == {
        "answers_rated": 1,
        "up": 0,
        "down": 1,
        "down_rate": 1.0,
    }
    assert bob_summary["by_lane"]["fast"] == {
        "answers_rated": 1,
        "up": 1,
        "down": 0,
        "down_rate": 0.0,
    }


# --- app/feedback.py: parsing helpers ---------------------------------------------


def test_parse_mode_used_forced() -> None:
    from app import feedback

    assert feedback.parse_mode_used("forced:claude-sonnet-5") == (
        "claude-sonnet-5",
        None,
    )


def test_parse_mode_used_free_lane() -> None:
    from app import feedback

    assert feedback.parse_mode_used("auto->free:groq/llama-3") == (
        "groq/llama-3",
        None,
    )


def test_parse_mode_used_category() -> None:
    from app import feedback

    assert feedback.parse_mode_used("auto->fast:coding") == (None, "coding")


def test_parse_mode_used_bare_tier() -> None:
    from app import feedback

    assert feedback.parse_mode_used("auto->smart") == (None, None)


def test_parse_mode_used_none() -> None:
    from app import feedback

    assert feedback.parse_mode_used(None) == (None, None)


def test_lane_from_mode_used_variants() -> None:
    from app import feedback

    assert feedback.lane_from_mode_used("auto->free:x") == "free"
    assert feedback.lane_from_mode_used("forced:x") == "forced"
    assert feedback.lane_from_mode_used("auto->budget") == "budget"
    assert feedback.lane_from_mode_used("auto->fast:coding") == "fast"
    assert feedback.lane_from_mode_used(None) is None


# --- duplicate/export/import field parity -----------------------------------------


def test_feedback_survives_duplicate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down", "reason": "Wrong"},
    )

    res = client.post(f"/v1/conversations/{cid}/duplicate")
    assert res.status_code == 200
    new_cid = res.json()["id"]
    duplicated = client.get(f"/v1/conversations/{new_cid}/messages").json()
    assistant = next(m for m in duplicated if m["role"] == "assistant")
    assert assistant["feedback"] == -1
    assert assistant["feedback_reason"] == "Wrong"


def test_feedback_survives_export_then_import(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch, mode_used="forced:gpt-5", model="gpt-5")
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "up"},
    )

    exported = client.get(f"/v1/conversations/{cid}/messages").json()
    payload = {
        "title": "Reimported",
        "messages": [
            {
                "role": m["role"],
                "content": m["content"],
                "mode_used": m["mode_used"],
                "model": m["model"],
                "feedback": m["feedback"],
                "feedback_reason": m["feedback_reason"],
            }
            for m in exported
        ],
    }
    res = client.post("/v1/conversations/import", json=payload)
    assert res.status_code == 200
    imported = client.get(f"/v1/conversations/{res.json()['id']}/messages").json()
    assistant = next(m for m in imported if m["role"] == "assistant")
    assert assistant["feedback"] == 1
    assert assistant["model"] == "gpt-5"


# --- share-link exclusion ----------------------------------------------------------


def test_feedback_excluded_from_shared_view(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid)
    message_id = _assistant_message_id(client, cid)
    client.put(
        f"/v1/conversations/{cid}/messages/{message_id}/feedback",
        json={"verdict": "down", "reason": "Wrong"},
    )

    share = client.post(f"/v1/conversations/{cid}/share", json={}).json()
    shared = client.get(f"/v1/shared/{share['token']}").json()
    for message in shared["messages"]:
        assert "feedback" not in message
        assert "feedback_reason" not in message
