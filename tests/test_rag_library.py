"""The RAG document library (app/rag_library.py, app/routers/library.py): an
opt-in, per-owner set of reference documents recalled via embedding
similarity and folded into a new turn's prompt — see app/memory.py for the
sibling cross-conversation-recall feature this mirrors.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, orchestrator, rag_library
from app.context_builder import _assemble_context_parts
from app.orchestrator import _library_block, _recall_library_context
from app.schemas import AskRequest, Mode
from app.telemetry import StageTimer, new_request_meta


@pytest.fixture()
def library_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_LIBRARY", "true")


def _data_url(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"data:text/plain;base64,{encoded}"


# --- config / flags ----------------------------------------------------------


def test_rag_library_enabled_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_LIBRARY", raising=False)
    assert rag_library.rag_library_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "TRUE"])
def test_rag_library_enabled_can_be_turned_on(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_LIBRARY", value)
    assert rag_library.rag_library_enabled() is True


def test_min_similarity_default_and_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAG_MIN_SIMILARITY", raising=False)
    assert rag_library.min_similarity() == 0.30
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "not-a-number")
    assert rag_library.min_similarity() == 0.30
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "1.5")  # out of (0, 1]
    assert rag_library.min_similarity() == 0.30
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "0.5")
    assert rag_library.min_similarity() == 0.5


def test_top_k_default_and_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_TOP_K", raising=False)
    assert rag_library.top_k() == 4
    monkeypatch.setenv("RAG_TOP_K", "0")
    assert rag_library.top_k() == 4
    monkeypatch.setenv("RAG_TOP_K", "not-a-number")
    assert rag_library.top_k() == 4
    monkeypatch.setenv("RAG_TOP_K", "2")
    assert rag_library.top_k() == 2


# --- chunk_text: pure, deterministic ------------------------------------------


def test_chunk_text_empty_and_whitespace_returns_nothing() -> None:
    assert rag_library.chunk_text("") == []
    assert rag_library.chunk_text("   \n  ") == []


def test_chunk_text_short_text_is_a_single_chunk() -> None:
    assert rag_library.chunk_text("hello world") == ["hello world"]


def test_chunk_text_is_deterministic() -> None:
    text = "abcdefgh " * 500
    assert rag_library.chunk_text(text) == rag_library.chunk_text(text)


def test_chunk_text_splits_long_text_into_overlapping_chunks() -> None:
    text = "x" * 2500
    chunks = rag_library.chunk_text(text, chunk_size=1000, overlap=150)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_chunk_text_raises_when_overlap_not_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError):
        rag_library.chunk_text("some text", chunk_size=100, overlap=100)


# --- extract_text --------------------------------------------------------------


def test_extract_text_plain_text_decodes_utf8() -> None:
    assert rag_library.extract_text("text/plain", b"hello world") == "hello world"


def test_extract_text_plain_text_replaces_bad_bytes() -> None:
    text = rag_library.extract_text("text/plain", b"good \xff\xfe bytes")
    assert "good" in text and "bytes" in text  # never raises


# --- retrieve() ------------------------------------------------------------------


def test_retrieve_returns_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_LIBRARY", raising=False)
    assert rag_library.retrieve([1.0, 0.0], None) == []


def test_retrieve_returns_empty_when_vector_is_none(
    db_path: Path, library_on: None
) -> None:
    assert rag_library.retrieve(None, None) == []


def test_retrieve_with_empty_library_returns_empty(
    db_path: Path, library_on: None
) -> None:
    assert rag_library.retrieve([1.0, 0.0], None) == []


def test_retrieve_finds_a_matching_chunk(db_path: Path, library_on: None) -> None:
    doc = database.library_document_create(None, "notes.txt", "text/plain", 100)
    database.library_chunk_add(
        doc["id"], None, 0, "the budget is $500", json.dumps([1.0, 0.0])
    )
    hits = rag_library.retrieve([1.0, 0.0], None)
    assert len(hits) == 1
    assert hits[0]["text"] == "the budget is $500"
    assert hits[0]["filename"] == "notes.txt"


def test_retrieve_skips_a_candidate_below_threshold(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "0.99")
    doc = database.library_document_create(None, "notes.txt", "text/plain", 100)
    database.library_chunk_add(doc["id"], None, 0, "text", json.dumps([1.0, 0.0]))
    hits = rag_library.retrieve([1.0, 0.3], None)
    assert hits == []


def test_retrieve_ranks_best_match_first_and_caps_at_top_k(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAG_MIN_SIMILARITY", "0.5")
    monkeypatch.setenv("RAG_TOP_K", "1")
    doc = database.library_document_create(None, "notes.txt", "text/plain", 100)
    database.library_chunk_add(doc["id"], None, 0, "far", json.dumps([1.0, 0.0]))
    database.library_chunk_add(doc["id"], None, 1, "close", json.dumps([0.9, 0.1]))
    hits = rag_library.retrieve([0.9, 0.1], None)
    assert len(hits) == 1
    assert hits[0]["text"] == "close"


def test_retrieve_is_scoped_by_owner(db_path: Path, library_on: None) -> None:
    alice_doc = database.library_document_create(
        "alice", "alice.txt", "text/plain", 100
    )
    database.library_chunk_add(
        alice_doc["id"], "alice", 0, "alice's secret", json.dumps([1.0, 0.0])
    )
    bob_hits = rag_library.retrieve([1.0, 0.0], "bob")
    assert bob_hits == []  # different owner -> no cross-user leak
    alice_hits = rag_library.retrieve([1.0, 0.0], "alice")
    assert len(alice_hits) == 1


def test_retrieve_does_not_leak_between_authenticated_and_anonymous(
    db_path: Path, library_on: None
) -> None:
    doc = database.library_document_create(None, "anon.txt", "text/plain", 100)
    database.library_chunk_add(doc["id"], None, 0, "anon text", json.dumps([1.0, 0.0]))
    alice_hits = rag_library.retrieve([1.0, 0.0], "alice")
    assert alice_hits == []


def test_retrieve_returns_empty_on_db_error(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_owner):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "library_chunks_list", boom)
    assert rag_library.retrieve([1.0, 0.0], None) == []


def test_retrieve_skips_a_chunk_with_malformed_embedding_json(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        database,
        "library_chunks_list",
        lambda owner: [{"filename": "x.txt", "text": "t", "embedding": "{bad"}],
    )
    assert rag_library.retrieve([1.0, 0.0], None) == []


# --- format_chunk / summarize_sources ------------------------------------------


def test_format_chunk_includes_filename_and_text() -> None:
    text = rag_library.format_chunk({"filename": "a.txt", "text": "hello"})
    assert text == "[a.txt]\nhello"


def test_summarize_sources_groups_by_filename_first_seen_order() -> None:
    chunks = [
        {"filename": "b.txt", "text": "1"},
        {"filename": "a.txt", "text": "2"},
        {"filename": "b.txt", "text": "3"},
    ]
    assert rag_library.summarize_sources(chunks) == [
        {"document": "b.txt", "snippet_count": 2},
        {"document": "a.txt", "snippet_count": 1},
    ]


def test_summarize_sources_empty_for_no_chunks() -> None:
    assert rag_library.summarize_sources([]) == []


# --- rag_library.recall: the embed -> retrieve -> format pipeline -------------


def test_recall_returns_zero_duration_and_empties_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAG_LIBRARY", raising=False)
    snippets, sources, duration_ms = rag_library.recall("q", None)
    assert snippets == []
    assert sources == []
    assert duration_ms >= 0


def test_recall_returns_snippets_and_sources_when_enabled(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda q: [1.0, 0.0])
    doc = database.library_document_create(None, "notes.txt", "text/plain", 100)
    database.library_chunk_add(
        doc["id"], None, 0, "budget info", json.dumps([1.0, 0.0])
    )
    snippets, sources, duration_ms = rag_library.recall("what's the budget?", None)
    assert snippets == ["[notes.txt]\nbudget info"]
    assert sources == [{"document": "notes.txt", "snippet_count": 1}]
    assert duration_ms >= 0


# --- _recall_library_context: the gate + its per-stage latency plumbing -------


def _timer() -> StageTimer:
    return StageTimer(new_request_meta())


def test_recall_library_context_skips_when_not_wanted(
    library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """regenerate/edit/workflow never pass recall_library=True."""
    monkeypatch.setattr(
        rag_library, "recall", lambda *a, **k: pytest.fail("should not retrieve")
    )
    assert _recall_library_context("analysis", "q", None, False, _timer()) == ([], [])


def test_recall_library_context_records_the_stage_timing(
    library_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "recall", lambda q, o: (["[a.txt]\nx"], [], 37))
    timer = _timer()
    _recall_library_context("analysis", "q", None, True, timer)
    assert ("library_embed", 37) in timer.stages()


def test_recall_library_context_stays_silent_when_the_library_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `library_embed=0ms` noise on every request in a deployment that
    never turned RAG_LIBRARY on."""
    monkeypatch.delenv("RAG_LIBRARY", raising=False)
    timer = _timer()
    assert _recall_library_context("analysis", "q", None, True, timer) == ([], [])
    assert [stage for stage, _ms in timer.stages()] == []


# --- context assembly: _library_block / apply_library_context ----------------


def test_library_block_empty_for_no_snippets() -> None:
    assert _library_block([]) == ""


def test_library_block_includes_every_snippet() -> None:
    block = _library_block(["[a.txt]\ntext a", "[b.txt]\ntext b"])
    assert "[a.txt]" in block
    assert "[b.txt]" in block


def test_apply_library_context_appends_to_both_prompt_shapes() -> None:
    question, cacheable_system = orchestrator.apply_library_context(
        ["[manual.txt]\nsome instructions"], "what's in the manual?", "SYSTEM"
    )
    assert question.startswith("what's in the manual?")
    assert "some instructions" in question
    assert cacheable_system is not None
    assert cacheable_system.startswith("SYSTEM")
    assert "some instructions" in cacheable_system


def test_apply_library_context_is_a_no_op_without_snippets() -> None:
    assert orchestrator.apply_library_context([], "q", "SYSTEM") == ("q", "SYSTEM")
    assert orchestrator.apply_library_context([], "q", None) == ("q", None)


def test_assemble_context_parts_no_library_no_history_stays_bare() -> None:
    parts = _assemble_context_parts(prior_messages=[], current_question="hi")
    assert parts.system_block == ""
    assert parts.recent_and_question == "hi"


# --- fresh-DB / migration: new tables exist ------------------------------------


def test_fresh_db_has_library_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "library_documents" in tables
    assert "library_chunks" in tables


def test_fresh_db_messages_table_has_library_sources_column(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "library_sources" in columns


# --- HTTP: upload / list / delete endpoints -------------------------------------


def test_upload_document_extracts_chunks_and_embeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    res = client.post(
        "/v1/library/documents",
        json={"filename": "notes.txt", "data": _data_url("hello world")},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["filename"] == "notes.txt"
    assert body["chunk_count"] == 1
    assert body["mime_type"] == "text/plain"


def test_list_documents_returns_uploaded_documents(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    client.post(
        "/v1/library/documents",
        json={"filename": "notes.txt", "data": _data_url("hello world")},
    )
    listed = client.get("/v1/library/documents").json()
    assert len(listed) == 1
    assert listed[0]["filename"] == "notes.txt"


def test_delete_document_removes_it_and_its_chunks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    uploaded = client.post(
        "/v1/library/documents",
        json={"filename": "notes.txt", "data": _data_url("hello world")},
    ).json()

    deleted = client.delete(f"/v1/library/documents/{uploaded['id']}")
    assert deleted.status_code == 200
    assert client.get("/v1/library/documents").json() == []


def test_delete_missing_document_returns_404(client: TestClient) -> None:
    res = client.delete("/v1/library/documents/999")
    assert res.status_code == 404


def test_upload_document_rejects_empty_extracted_text(client: TestClient) -> None:
    res = client.post(
        "/v1/library/documents",
        json={"filename": "empty.txt", "data": _data_url("   ")},
    )
    assert res.status_code == 422


def test_upload_document_reuses_file_attachment_validation(client: TestClient) -> None:
    """Malformed data URLs are rejected by FileAttachment's own validator —
    the same scrutiny a per-message attachment gets (see FileAttachment)."""
    res = client.post(
        "/v1/library/documents",
        json={"filename": "bad.txt", "data": "not-a-data-url"},
    )
    assert res.status_code == 422


def test_upload_document_returns_502_when_every_embed_call_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: None)
    res = client.post(
        "/v1/library/documents",
        json={"filename": "notes.txt", "data": _data_url("hello world")},
    )
    assert res.status_code == 502
    # No orphaned document left behind.
    assert client.get("/v1/library/documents").json() == []


# --- app_doc_files / seed-app-docs ------------------------------------------


def test_app_doc_files_reads_the_real_docs_directory() -> None:
    files = rag_library.app_doc_files()
    names = [name for name, _text in files]
    assert "features.md" in names
    assert "api-reference.md" in names
    assert all(name.endswith(".md") for name in names)
    assert all(text.strip() for _name, text in files)


def test_app_doc_files_empty_for_a_missing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rag_library, "APP_DOCS_DIR", tmp_path / "no-such-dir")
    assert rag_library.app_doc_files() == []


def test_seed_app_docs_ingests_the_real_docs_directory(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    res = client.post("/v1/library/seed-app-docs")
    assert res.status_code == 201
    body = res.json()
    real_docs = rag_library.app_doc_files()
    assert len(body) == len(real_docs)
    seeded_names = {doc["filename"] for doc in body}
    assert seeded_names == {name for name, _text in real_docs}
    assert all(doc["mime_type"] == "text/markdown" for doc in body)
    listed = client.get("/v1/library/documents").json()
    assert len(listed) == len(real_docs)


def test_seed_app_docs_is_idempotent_per_filename(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    first = client.post("/v1/library/seed-app-docs")
    assert len(first.json()) == len(rag_library.app_doc_files())

    second = client.post("/v1/library/seed-app-docs")
    assert second.status_code == 201
    assert second.json() == []  # every doc already present — nothing re-embedded
    listed = client.get("/v1/library/documents").json()
    assert len(listed) == len(rag_library.app_doc_files())


def test_seed_app_docs_scoped_by_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    database.library_document_create("alice", "features.md", "text/markdown", 10)
    # The default (no auth configured) test client has no owner, so alice's
    # already-seeded "features.md" must not block this owner from seeding
    # the same filename for themselves.
    res = client.post("/v1/library/seed-app-docs")
    seeded_names = {doc["filename"] for doc in res.json()}
    assert "features.md" in seeded_names


def test_seed_app_docs_requires_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.post("/v1/library/seed-app-docs").status_code == 401


def test_library_endpoints_are_scoped_by_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_library, "embed", lambda chunk: [1.0, 0.0])
    database.library_document_create("alice", "alice.txt", "text/plain", 10)
    # The default (no auth configured) test client has no owner, so it must
    # never see alice's document.
    assert client.get("/v1/library/documents").json() == []


def test_library_endpoints_require_auth(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "secret-token")
    assert client.get("/v1/library/documents").status_code == 401
    ok = client.get(
        "/v1/library/documents", headers={"Authorization": "Bearer secret-token"}
    )
    assert ok.status_code == 200


# --- the category gate: end to end through the real orchestrator --------------


@pytest.fixture()
def seeded_library(
    db_path: Path, library_on: None, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """A one-chunk library plus a stubbed embedder, and the list every
    `embed` call's question lands in — so a test can assert that retrieval
    was skipped, not merely that nothing came back."""
    embed_calls: list[str] = []
    monkeypatch.setattr(
        rag_library, "embed", lambda q: embed_calls.append(q) or [1.0, 0.0]
    )
    doc = database.library_document_create(None, "routing.md", "text/plain", 100)
    database.library_chunk_add(
        doc["id"], None, 0, "the router picks a cheap model", json.dumps([1.0, 0.0])
    )
    return embed_calls


def _route_as(monkeypatch: pytest.MonkeyPatch, category: str) -> list[str]:
    """Pin the router to `category` without a classifier call, and capture
    every prompt handed to the model. Returns that capture list."""
    from app.routing import RouteDecision

    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            model="fast-x",
            mode_used=f"auto->fast:{category}",
            notes="n",
            max_output_tokens=100,
            reasoning_effort="low",
            category=category,
        ),
    )
    prompts: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        prompts.append(str(kwargs["question"]))
        return "answered"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    return prompts


def test_a_transform_task_never_retrieves_and_carries_no_provenance(
    monkeypatch: pytest.MonkeyPatch, seeded_library: list[str]
) -> None:
    """The bug this gate exists for: a "rewrite this, translate it, lay it
    out as a table" request whose SUBJECT happened to match the library
    pulled those documents in, appended a note about them, and claimed them
    as sources — on a task whose entire input was the text to transform."""
    prompts = _route_as(monkeypatch, "simple_transform")

    result = orchestrator.run_orchestrator(
        AskRequest(question="rewrite this in plain English: ...", mode=Mode.auto),
        recall_library=True,
    )

    assert seeded_library == []  # no embedding call, so no library scan either
    assert result.library_sources is None
    assert "routing.md" not in prompts[0]
    assert "the router picks a cheap model" not in prompts[0]


def test_a_task_that_needs_the_library_still_retrieves(
    monkeypatch: pytest.MonkeyPatch, seeded_library: list[str]
) -> None:
    """The converse, so the gate can't quietly become "retrieval off". Same
    library, same request shape — only the classifier's category differs."""
    prompts = _route_as(monkeypatch, "analysis")

    result = orchestrator.run_orchestrator(
        AskRequest(question="how does the router choose a model?", mode=Mode.auto),
        recall_library=True,
    )

    assert seeded_library == ["how does the router choose a model?"]
    assert result.library_sources is not None
    assert [s.document for s in result.library_sources] == ["routing.md"]
    assert "the router picks a cheap model" in prompts[0]


def test_the_gate_applies_to_the_streaming_path_too(
    monkeypatch: pytest.MonkeyPatch, seeded_library: list[str]
) -> None:
    prompts: list[str] = []
    from app.routing import RouteDecision

    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator,
        "decide_route",
        lambda *a, **k: RouteDecision(
            model="fast-x",
            mode_used="auto->fast:summarization",
            notes="n",
            max_output_tokens=100,
            reasoning_effort="low",
            category="summarization",
        ),
    )

    def fake_stream_model(**kwargs: object):
        prompts.append(str(kwargs["question"]))
        yield "answered"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        orchestrator.stream_orchestrator(
            AskRequest(question="summarise the paragraph below: ...", mode=Mode.auto),
            recall_library=True,
        )
    )

    assert seeded_library == []
    done = next(e for e in events if e["event"] == "done")
    assert "library_sources" not in done["data"]
    assert "routing.md" not in prompts[0]


# --- flag-off / scope: who asks for library recall at all ----------------------


def test_ask_conversation_asks_the_orchestrator_to_recall_the_library(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """The ask path opts in; the recall itself (and its category gate) lives
    inside the orchestrator, so this is all the router layer decides."""
    import app.routers.messages as messages_module

    captured: list[object] = []

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        captured.append(kwargs.get("recall_library"))
        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert captured == [True]


def test_ask_conversation_persists_library_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    import app.routers.messages as messages_module

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse, LibrarySource

        return AskResponse(
            answer="the widget costs $10",
            mode_used="auto->fast",
            notes="n",
            library_sources=[LibrarySource(document="manual.txt", snippet_count=1)],
        )

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    res = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "how much is the widget?"}
    )

    assert res.json()["library_sources"] == [
        {"document": "manual.txt", "snippet_count": 1}
    ]
    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_message = next(m for m in persisted if m["role"] == "assistant")
    assert assistant_message["library_sources"] == [
        {"document": "manual.txt", "snippet_count": 1}
    ]


def test_regenerate_never_recalls_from_library(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, library_on: None
) -> None:
    """Scope guard: like memory, the library is only ever recalled from the
    primary ask/ask-stream path — never regenerate/edit."""
    import app.routers.messages as messages_module

    captured: list[object] = []

    def fake_run(req, routing_question=None, owner=None, **kwargs):
        from app.schemas import AskResponse

        captured.append(kwargs.get("recall_library"))
        return AskResponse(answer="a", mode_used="auto->fast", notes="n")

    monkeypatch.setattr(messages_module, "run_orchestrator", fake_run)

    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    captured.clear()

    client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert captured == [None]


def test_rag_library_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "RAG_LIBRARY")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
