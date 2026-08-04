"""Implicit correction tracking (app/correction_tracking.py, correction_log)
— a soft, measurement-only signal distinct from explicit 👍/👎 feedback. See
app/correction_tracking.py's module docstring for the full design rationale.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routers.messages as _messages
from app import correction_tracking, database
from app.schemas import AskRequest, AskResponse


def _stub_run_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    mode_used: str = "auto->fast:coding",
    model: str | None = "gpt-5-mini",
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

    monkeypatch.setattr(_messages, "run_orchestrator", fake_run_orchestrator)


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


# --- schema ------------------------------------------------------------------


def _column_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def test_fresh_db_has_the_correction_log_table_and_index(db_path) -> None:
    columns = _column_names(db_path, "correction_log")
    assert columns == {
        "id",
        "owner",
        "message_id",
        "model",
        "mode_used",
        "category",
        "created_at",
    }
    assert "idx_correction_log_created_at" in _index_names(db_path, "correction_log")


# --- looks_like_correction: should-flag phrases -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "That's not what I asked, please try again.",
        "that is not what i meant",
        "You didn't answer that.",
        "you didn't answer my question at all",
        "That doesn't answer my question.",
        "Wrong tool - I needed a calculation, not a search.",
        "I didn't ask for a poem.",
        "You misunderstood my question, let me clarify.",
    ],
)
def test_looks_like_correction_flags_curated_phrases(text: str) -> None:
    assert correction_tracking.looks_like_correction(text) is True


# --- looks_like_correction: must-not-flag traps -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Bare "wrong" about a third party / document content, not the answer.
        "The vendor's invoice total is wrong, can you help me dispute it?",
        "The report says the wrong year for that event.",
        # Bare "no" as conversational filler.
        "No worries, thanks for the help!",
        "No, that's totally fine, go ahead.",
        # A correction phrase that only appears INSIDE a quote (relaying
        # someone else's words), not the caller's own correction.
        'My teammate said "that\'s not what I asked" in the review thread — '
        "any idea how to phrase the ticket better?",
        # An ordinary follow-up question.
        "Can you also add error handling to that function?",
        "Thanks, that's exactly what I needed.",
    ],
)
def test_looks_like_correction_ignores_traps(text: str) -> None:
    assert correction_tracking.looks_like_correction(text) is False


def test_looks_like_correction_only_checks_the_first_sentence() -> None:
    text = (
        "Here's some more context on the project. "
        "that's not what i asked, by the way, was in an earlier message."
    )
    assert correction_tracking.looks_like_correction(text) is False


def test_looks_like_correction_empty_text() -> None:
    assert correction_tracking.looks_like_correction("") is False
    assert correction_tracking.looks_like_correction(None) is False  # type: ignore[arg-type]


# --- record_if_correction: gating ---------------------------------------------


def test_record_if_correction_noop_without_prior_messages(db_path) -> None:
    correction_tracking.record_if_correction(None, [], "that's not what i asked")
    assert database.correction_log_entries(None, days=1) == []


def test_record_if_correction_noop_when_previous_message_is_user(db_path) -> None:
    prior = [{"id": 1, "role": "user", "mode_used": None, "model": None}]
    correction_tracking.record_if_correction(None, prior, "that's not what i asked")
    assert database.correction_log_entries(None, days=1) == []


def test_record_if_correction_noop_for_an_ordinary_followup(db_path) -> None:
    prior = [
        {
            "id": 1,
            "role": "assistant",
            "mode_used": "auto->fast:coding",
            "model": "gpt-5-mini",
        }
    ]
    correction_tracking.record_if_correction(None, prior, "Can you add tests too?")
    assert database.correction_log_entries(None, days=1) == []


def test_record_if_correction_flags_the_previous_assistant_message(db_path) -> None:
    prior = [
        {
            "id": 42,
            "role": "assistant",
            "mode_used": "auto->fast:coding",
            "model": "gpt-5-mini",
        }
    ]
    correction_tracking.record_if_correction("alice", prior, "That's not what I asked.")
    entries = database.correction_log_entries("alice", days=1)
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5-mini"
    assert entries[0]["category"] == "coding"
    assert entries[0]["mode_used"] == "auto->fast:coding"


def test_record_if_correction_respects_the_disabled_flag(
    db_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORRECTION_TRACKING", "false")
    prior = [
        {
            "id": 42,
            "role": "assistant",
            "mode_used": "auto->fast:coding",
            "model": "gpt-5-mini",
        }
    ]
    correction_tracking.record_if_correction(None, prior, "That's not what I asked.")
    assert database.correction_log_entries(None, days=1) == []


def test_correction_tracking_defaults_on(db_path) -> None:
    assert correction_tracking.correction_tracking_enabled() is True


# --- record_if_correction: never writes to the feedback ledger ----------------


def test_record_if_correction_never_touches_feedback_log(db_path) -> None:
    prior = [
        {
            "id": 42,
            "role": "assistant",
            "mode_used": "auto->fast:coding",
            "model": "gpt-5-mini",
        }
    ]
    correction_tracking.record_if_correction(None, prior, "That's not what I asked.")
    from app import feedback

    assert feedback.summarize(None, days=1) == {
        "by_model": {},
        "by_category": {},
        "by_lane": {},
    }


# --- wired end-to-end via the /ask endpoint -----------------------------------


def test_ask_flags_the_prior_answer_on_a_correction_followup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid, "What's the weather like?")

    _ask(client, cid, "That's not what I asked.")

    entries = database.correction_log_entries(None, days=1)
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5-mini"
    assert entries[0]["category"] == "coding"


def test_ask_does_not_flag_an_ordinary_followup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid, "What's the weather like?")
    _ask(client, cid, "Can you also give me the forecast for tomorrow?")

    assert database.correction_log_entries(None, days=1) == []


def test_ask_does_not_flag_the_very_first_message_in_a_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid, "That's not what I asked.")

    assert database.correction_log_entries(None, days=1) == []


def test_ask_stream_also_flags_a_correction_followup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """record_if_correction runs synchronously before the streaming response
    is even built, using the SAME prior_messages the non-streaming route
    reads — so a real, already-persisted prior assistant message (inserted
    directly, no need to stub the model call) is enough to prove the stream
    route is wired identically, without re-testing the whole
    _stream_and_persist pipeline."""

    def fake_stream(*args, **kwargs):
        from fastapi.responses import StreamingResponse

        def gen():
            yield 'data: {"event": "done", "data": {}}\n\n'

        return StreamingResponse(gen(), media_type="text/event-stream")

    import app.routers.messages.ask as ask_module

    monkeypatch.setattr(ask_module, "_stream_and_persist", fake_stream)

    cid = _create(client)
    database.add_message(
        conversation_id=cid,
        role="assistant",
        content="prior answer",
        mode_used="auto->fast:coding",
        model="gpt-5-mini",
    )

    client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "That's not what I asked."},
    )

    entries = database.correction_log_entries(None, days=1)
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-5-mini"
    assert entries[0]["category"] == "coding"


# --- summarize() ---------------------------------------------------------------


def test_summarize_computes_rate_per_model_category_lane(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(
        monkeypatch, mode_used="auto->fast:coding", model="gpt-5-mini"
    )
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "That's not what I asked.")
    _ask(client, cid, "Another ordinary follow-up.")

    summary = correction_tracking.summarize(None, days=1)
    assert summary["overall"]["flagged"] == 1
    assert summary["overall"]["answers"] == 3
    assert summary["overall"]["correction_rate"] == pytest.approx(1 / 3)

    assert summary["by_model"]["gpt-5-mini"]["flagged"] == 1
    assert summary["by_model"]["gpt-5-mini"]["answers"] == 3

    assert summary["by_category"]["coding"]["flagged"] == 1
    assert summary["by_lane"]["fast"]["flagged"] == 1


def test_summarize_empty_when_nothing_happened(db_path) -> None:
    summary = correction_tracking.summarize(None, days=1)
    assert summary == {
        "overall": {"flagged": 0, "answers": 0, "correction_rate": 0.0},
        "by_model": {},
        "by_category": {},
        "by_lane": {},
    }


def test_summarize_respects_the_days_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    _ask(client, cid, "first question")
    _ask(client, cid, "That's not what I asked.")

    conn = sqlite3.connect(database._db_path())
    conn.execute("UPDATE correction_log SET created_at = datetime('now', '-10 days')")
    conn.execute("UPDATE messages SET created_at = datetime('now', '-10 days')")
    conn.commit()
    conn.close()

    summary = correction_tracking.summarize(None, days=1)
    assert summary["overall"] == {"flagged": 0, "answers": 0, "correction_rate": 0.0}


def test_summarize_scoped_by_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "correction-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    _stub_run_orchestrator(monkeypatch)

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {alice}"}

    cid = client.post(
        "/v1/conversations", json={"title": "alice's chat"}, headers=headers
    ).json()["id"]
    client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "hi"}, headers=headers
    )
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "That's not what I asked."},
        headers=headers,
    )

    assert correction_tracking.summarize("alice", days=1)["overall"]["flagged"] == 1
    assert correction_tracking.summarize(None, days=1)["overall"]["flagged"] == 0
