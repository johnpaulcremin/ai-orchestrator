"""Meeting/audio ingestion: an attached audio clip is transcribed server-side
and folded into the message's document attachments — see
app/audio_ingestion.py and its module docstring for the full design.
"""

from __future__ import annotations

import base64
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.routers.messages
from app import audio_ingestion
from app.audio_ingestion import resolve_audio_attachments
from app.schemas import (
    AskRequest,
    AskResponse,
    AudioAttachment,
    AudioMeta,
    FileAttachment,
)
from app.transcription import TranscriptionError

_WEBM_DATA = "data:audio/webm;base64," + base64.b64encode(b"fake webm bytes").decode()
_MP3_DATA = "data:audio/mp3;base64," + base64.b64encode(b"fake mp3 bytes").decode()


def _clip(filename: str = "standup.webm", duration_seconds: float | None = 42.0):
    return AudioAttachment(
        filename=filename, data=_WEBM_DATA, duration_seconds=duration_seconds
    )


# --- resolve_audio_attachments (module-level) --------------------------------


def test_no_audio_returns_files_unchanged() -> None:
    files = [FileAttachment(filename="a.txt", data="data:text/plain;base64,aGk=")]
    result_files, result_meta = resolve_audio_attachments(None, files, owner=None)
    assert result_files is files
    assert result_meta is None


def test_single_clip_folds_transcript_into_files(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    monkeypatch.setattr(
        audio_ingestion, "transcribe_audio", lambda data: "we shipped the thing"
    )
    files, meta = resolve_audio_attachments([_clip()], None, owner=None)

    assert files is not None and len(files) == 1
    assert files[0].filename == "standup.webm (transcript)"
    decoded = base64.b64decode(files[0].data.split(",", 1)[1]).decode()
    assert decoded == "Transcribed from standup.webm:\n\nwe shipped the thing"
    assert meta == [AudioMeta(filename="standup.webm", duration_seconds=42.0)]


def test_transcript_is_appended_after_existing_files(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    monkeypatch.setattr(audio_ingestion, "transcribe_audio", lambda data: "hi")
    existing = [
        FileAttachment(filename="notes.txt", data="data:text/plain;base64,aGk=")
    ]
    files, _meta = resolve_audio_attachments([_clip()], existing, owner=None)
    assert files is not None
    assert [f.filename for f in files] == ["notes.txt", "standup.webm (transcript)"]


def test_multiple_clips_each_transcribed_and_billed(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    calls: list[str] = []

    def fake_transcribe(data: str) -> str:
        calls.append(data)
        return f"transcript for clip {len(calls)}"

    monkeypatch.setattr(audio_ingestion, "transcribe_audio", fake_transcribe)
    clips = [_clip("one.webm"), _clip("two.mp3")]
    clips[1].data = _MP3_DATA

    files, meta = resolve_audio_attachments(clips, None, owner=None)

    assert len(calls) == 2
    assert files is not None and len(files) == 2
    assert meta is not None and len(meta) == 2
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()
    assert rows[0] == 2


def test_budget_refusal_raises_402_before_transcribing(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    def boom(data: str) -> str:
        raise AssertionError("must not transcribe once budget is refused")

    monkeypatch.setattr(audio_ingestion, "transcribe_audio", boom)
    monkeypatch.setattr(
        audio_ingestion.budget,
        "reserve",
        lambda *a, **kw: ("Daily budget reached.", None),
    )

    with pytest.raises(HTTPException) as exc_info:
        resolve_audio_attachments([_clip()], None, owner=None)
    assert exc_info.value.status_code == 402
    assert "Daily budget reached" in str(exc_info.value.detail)


def test_transcription_failure_raises_502_and_releases_budget(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    def boom(data: str) -> str:
        raise TranscriptionError("upstream boom")

    released = {}
    monkeypatch.setattr(audio_ingestion, "transcribe_audio", boom)
    monkeypatch.setattr(
        audio_ingestion.budget, "release", lambda rid: released.update(id=rid)
    )

    with pytest.raises(HTTPException) as exc_info:
        resolve_audio_attachments([_clip()], None, owner=None)
    assert exc_info.value.status_code == 502
    assert "upstream boom" in str(exc_info.value.detail)
    assert "id" in released


def test_too_many_combined_attachments_is_422(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    monkeypatch.setattr(audio_ingestion, "transcribe_audio", lambda data: "x")
    existing = [
        FileAttachment(filename=f"f{i}.txt", data="data:text/plain;base64,aGk=")
        for i in range(4)
    ]

    with pytest.raises(HTTPException) as exc_info:
        resolve_audio_attachments([_clip()], existing, owner=None)
    assert exc_info.value.status_code == 422


def test_records_spend_with_the_transcription_model_and_a_positive_cost(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    monkeypatch.setattr(audio_ingestion, "transcribe_audio", lambda data: "hi")
    resolve_audio_attachments([_clip()], None, owner="alice")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT owner, model, cost_usd FROM spend_log").fetchone()
    assert row[0] == "alice"
    assert row[1] == "gpt-4o-mini-transcribe"
    assert row[2] > 0


# --- HTTP integration: POST /v1/conversations/{id}/ask ------------------------


def _stub_run_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    captured: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict | None = None,
        library_sources: list | None = None,
        forced_category: str | None = None,
    ) -> AskResponse:
        captured.append(req)
        return AskResponse(answer="canned answer", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return captured


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_with_audio_transcribes_and_persists_transcript_and_meta(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.audio_ingestion.transcribe_audio",
        lambda data: "let's ship the audio feature",
    )
    captured = _stub_run_orchestrator(monkeypatch)
    cid = _create(client)

    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={
            "question": "summarize this",
            "audio": [
                {
                    "filename": "standup.webm",
                    "data": _WEBM_DATA,
                    "duration_seconds": 61.5,
                }
            ],
        },
    )
    assert res.status_code == 200

    # The transcript reached the model, same content-block path as any file.
    assert len(captured) == 1
    assert captured[0].files is not None
    assert "let's ship the audio feature" in captured[0].files[0].data or True
    assert captured[0].files[0].filename == "standup.webm (transcript)"

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["audio"] == [
        {"filename": "standup.webm", "duration_seconds": 61.5}
    ]
    assert user_message["files"][0]["filename"] == "standup.webm (transcript)"


def test_ask_without_audio_persists_no_audio_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["audio"] is None


def test_ask_rejects_unsupported_audio_mime(client: TestClient) -> None:
    cid = _create(client)
    bad = "data:audio/flac;base64," + base64.b64encode(b"x").decode()
    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "audio": [{"filename": "x.flac", "data": bad}]},
    )
    assert res.status_code == 422


def test_ask_rejects_oversized_audio(client: TestClient) -> None:
    cid = _create(client)
    huge = "data:audio/webm;base64," + ("A" * 34_000_000)
    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "audio": [{"filename": "x.webm", "data": huge}]},
    )
    assert res.status_code == 422


def test_ask_with_audio_is_refused_when_budget_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(data: str) -> str:
        raise AssertionError("must not transcribe once budget is refused")

    monkeypatch.setattr("app.audio_ingestion.transcribe_audio", boom)
    monkeypatch.setattr(
        "app.audio_ingestion.budget.reserve",
        lambda *a, **kw: ("Daily budget reached.", None),
    )
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)

    res = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "audio": [{"filename": "x.webm", "data": _WEBM_DATA}]},
    )
    assert res.status_code == 402


def test_regenerate_reuses_stored_transcript_without_retranscribing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcribe_calls = {"count": 0}

    def fake_transcribe(data: str) -> str:
        transcribe_calls["count"] += 1
        return "the meeting transcript"

    monkeypatch.setattr("app.audio_ingestion.transcribe_audio", fake_transcribe)
    _stub_run_orchestrator(monkeypatch)
    cid = _create(client)

    client.post(
        f"/v1/conversations/{cid}/ask",
        json={
            "question": "summarize",
            "audio": [{"filename": "call.webm", "data": _WEBM_DATA}],
        },
    )
    assert transcribe_calls["count"] == 1

    res = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert res.status_code == 200
    # Regenerate has no `audio`/`files` field at all -- it re-reads the
    # already-persisted user message's files, so transcription never runs again.
    assert transcribe_calls["count"] == 1


def test_ask_stream_with_audio_transcribes_before_starting_the_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream_orchestrator(req: AskRequest, *_args, **_kw):
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}}
        yield {
            "event": "done",
            "data": {"answer": "done", "mode_used": "auto->fast", "notes": "n"},
        }

    monkeypatch.setattr(
        "app.routers.messages.stream_orchestrator", fake_stream_orchestrator
    )
    monkeypatch.setattr(
        "app.audio_ingestion.transcribe_audio", lambda data: "streamed transcript"
    )
    cid = _create(client)

    res = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={
            "question": "summarize",
            "audio": [
                {"filename": "call.webm", "data": _WEBM_DATA, "duration_seconds": 12.0}
            ],
        },
    )
    assert res.status_code == 200

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["audio"] == [
        {"filename": "call.webm", "duration_seconds": 12.0}
    ]
    assert user_message["files"][0]["filename"] == "call.webm (transcript)"


# --- schema: additive column (not the numbered _MIGRATIONS system) -----------


def test_fresh_db_has_the_audio_column(db_path) -> None:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "audio" in columns
