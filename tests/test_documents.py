"""Document input: FileAttachment validation, per-provider document-block
construction (OpenAI/Anthropic/LiteLLM), cache-skip, fallback threading, and
end-to-end persistence (including reuse on regenerate).
"""

from __future__ import annotations

import base64
import json
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.orchestrator as orchestrator
from app import providers
from app.database import add_message, create_conversation, list_messages
from app.orchestrator import _build_input, _cache_key
from app.schemas import AskRequest, FileAttachment, Mode

_PDF_DATA = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()
_TEXT_DATA = (
    "data:text/plain;base64," + base64.b64encode(b"hello from a text file").decode()
)


# --- schemas: FileAttachment / AskRequest.files validation --------------------


def test_ask_request_accepts_valid_pdf_attachment() -> None:
    req = AskRequest(
        question="summarize this",
        files=[FileAttachment(filename="a.pdf", data=_PDF_DATA)],
    )
    assert req.files is not None
    assert req.files[0].filename == "a.pdf"


def test_ask_request_accepts_valid_text_attachment() -> None:
    req = AskRequest(
        question="summarize this",
        files=[FileAttachment(filename="notes.txt", data=_TEXT_DATA)],
    )
    assert req.files is not None
    assert req.files[0].filename == "notes.txt"


def test_ask_request_empty_files_list_becomes_none() -> None:
    req = AskRequest(question="hi", files=[])
    assert req.files is None


def test_ask_request_no_files_defaults_to_none() -> None:
    req = AskRequest(question="hi")
    assert req.files is None


def test_ask_request_rejects_too_many_files() -> None:
    with pytest.raises(ValidationError):
        AskRequest(
            question="summarize",
            files=[FileAttachment(filename="a.pdf", data=_PDF_DATA)] * 5,
        )


def test_ask_request_accepts_max_files() -> None:
    req = AskRequest(
        question="summarize",
        files=[FileAttachment(filename="a.pdf", data=_PDF_DATA)] * 4,
    )
    assert req.files is not None
    assert len(req.files) == 4


def test_file_attachment_rejects_empty_filename() -> None:
    with pytest.raises(ValidationError):
        FileAttachment(filename="   ", data=_PDF_DATA)


def test_file_attachment_rejects_unsupported_mime() -> None:
    bad = "data:application/zip;base64," + base64.b64encode(b"x").decode()
    with pytest.raises(ValidationError):
        FileAttachment(filename="a.zip", data=bad)


def test_file_attachment_rejects_bare_http_url() -> None:
    """Only data: URLs are accepted — a remote URL would have the PROVIDER
    fetch it server-side on our behalf, an SSRF vector via a third party."""
    with pytest.raises(ValidationError):
        FileAttachment(filename="a.pdf", data="https://example.com/a.pdf")


def test_file_attachment_rejects_oversized_file() -> None:
    huge = "data:application/pdf;base64," + ("A" * 21_000_000)
    with pytest.raises(ValidationError):
        FileAttachment(filename="a.pdf", data=huge)


# --- providers: _anthropic_document_block --------------------------------------


def test_anthropic_document_block_pdf_stays_base64() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    block = providers._anthropic_document_block(file)
    assert block is not None
    assert block["type"] == "document"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "application/pdf"
    assert block["title"] == "a.pdf"


def test_anthropic_document_block_text_plain_is_decoded_to_raw_text() -> None:
    """Unlike every other content type here, Claude's plain-text document
    source wants the RAW text, not base64."""
    file = FileAttachment(filename="notes.txt", data=_TEXT_DATA)
    block = providers._anthropic_document_block(file)
    assert block is not None
    assert block["source"]["type"] == "text"
    assert block["source"]["data"] == "hello from a text file"


def test_anthropic_document_block_malformed_data_url_returns_none() -> None:
    file = types.SimpleNamespace(filename="a.pdf", data="not-a-data-url")
    assert providers._anthropic_document_block(file) is None


def test_anthropic_document_block_unsupported_mime_returns_none() -> None:
    file = types.SimpleNamespace(
        filename="a.png",
        data="data:image/png;base64," + base64.b64encode(b"x").decode(),
    )
    assert providers._anthropic_document_block(file) is None


# --- providers: _anthropic_content / _litellm_content with files -------------


def test_anthropic_content_no_files_returns_plain_string() -> None:
    assert providers._anthropic_content("hi", None, None) == "hi"
    assert providers._anthropic_content("hi", None, []) == "hi"


def test_anthropic_content_builds_text_and_document_blocks() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    content = providers._anthropic_content("summarize", None, [file])
    assert content[0] == {"type": "text", "text": "summarize"}
    assert content[1]["type"] == "document"


def test_anthropic_content_combines_images_and_files() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    content = providers._anthropic_content(
        "look at these", ["data:image/png;base64,aGVsbG8="], [file]
    )
    types_seen = [block["type"] for block in content[1:]]
    assert types_seen == ["image", "document"]


def test_litellm_content_no_files_returns_plain_string() -> None:
    assert providers._litellm_content("hi", None, None) == "hi"


def test_litellm_content_builds_text_and_file_blocks() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    content = providers._litellm_content("summarize", None, [file])
    assert content[0] == {"type": "text", "text": "summarize"}
    assert content[1] == {
        "type": "file",
        "file": {"filename": "a.pdf", "file_data": _PDF_DATA},
    }


# --- providers: call_anthropic / call_litellm actually forward files ---------


def test_call_anthropic_forwards_files(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    providers.call_anthropic("claude-x", "summarize", 100, 30.0, files=[file])
    content = captured["messages"][0]["content"]
    assert content[1]["type"] == "document"


def test_call_litellm_forwards_files(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    providers.call_litellm(
        "gemini/gemini-2.5-pro", "summarize", 128, 30.0, files=[file]
    )
    content = captured["messages"][0]["content"]
    assert content[1]["type"] == "file"


# --- orchestrator: _build_input with files -------------------------------------


def test_build_input_no_files_returns_plain_string() -> None:
    assert _build_input("hi", None, None) == "hi"


def test_build_input_with_files_builds_multipart_content() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    result = _build_input("summarize", None, [file])
    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "summarize"},
                {"type": "input_file", "filename": "a.pdf", "file_data": _PDF_DATA},
            ],
        }
    ]


def test_build_input_combines_images_and_files() -> None:
    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    result = _build_input("look", ["data:image/png;base64,aGVsbG8="], [file])
    content = result[0]["content"]
    assert [c["type"] for c in content] == ["input_text", "input_image", "input_file"]


# --- orchestrator: _cache_key skips caching when files are attached ----------


def test_cache_key_none_when_files_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    req = AskRequest(
        question="summarize", files=[FileAttachment(filename="a.pdf", data=_PDF_DATA)]
    )
    assert _cache_key(req) is None


# --- orchestrator: files threaded to _call_model on primary + fallback -------


def test_run_orchestrator_threads_files_to_call_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("files", kwargs["files"]) or "ok",
    )

    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    orchestrator.run_orchestrator(
        AskRequest(question="summarize", mode=Mode.smart, files=[file])
    )
    assert seen["files"] == [file]


def test_run_orchestrator_fallback_also_gets_files(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike web_search/actions/generated-images, documents work across every
    provider, so — unlike those — files are kept on the fallback call."""
    import httpx
    from openai import APIError

    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_fallback_models", lambda *a, **k: ["fallback-model"]
    )

    seen = {}

    def fake_call_model(**kwargs):
        if kwargs["model"] != "fallback-model":
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            raise APIError("boom", request=request, body=None)
        seen["files"] = kwargs["files"]
        return "recovered"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    result = orchestrator.run_orchestrator(
        AskRequest(question="summarize", mode=Mode.smart, files=[file])
    )
    assert result.answer == "recovered"
    assert seen["files"] == [file]


def test_stream_orchestrator_threads_files(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_stream_model(**kwargs):
        seen["files"] = kwargs["files"]
        yield "ok"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    file = FileAttachment(filename="a.pdf", data=_PDF_DATA)
    list(
        orchestrator.stream_orchestrator(
            AskRequest(question="summarize", mode=Mode.smart, files=[file])
        )
    )
    assert seen["files"] == [file]


def test_run_orchestrator_no_files_passes_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("files", kwargs["files"]) or "ok",
    )

    orchestrator.run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["files"] is None


# --- database: files persistence -----------------------------------------------


def test_add_message_and_list_messages_roundtrip_files(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="user",
        content="summarize this",
        files=json.dumps([{"filename": "a.pdf", "data": _PDF_DATA}]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["files"]) == [
        {"filename": "a.pdf", "data": _PDF_DATA}
    ]


def test_add_message_without_files_stores_null(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="user", content="hi")
    messages = list_messages(conv["id"])
    assert messages[0]["files"] is None


# --- HTTP integration: files persist + reach the orchestrator + regenerate ---


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_attached_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    captured_files: list = []

    def fake_run(req, routing_question=None, owner=None, history=""):
        captured_files.append(req.files)
        return AskResponse(answer="It's a PDF.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={
            "question": "summarize this",
            "files": [{"filename": "a.pdf", "data": _PDF_DATA}],
        },
    )
    assert r.status_code == 200
    assert captured_files[-1][0].filename == "a.pdf"

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message = next(m for m in persisted if m["role"] == "user")
    assert user_message["files"] == [{"filename": "a.pdf", "data": _PDF_DATA}]


def test_ask_endpoint_rejects_unsupported_file_mime(client: TestClient) -> None:
    bad = "data:application/zip;base64," + base64.b64encode(b"x").decode()
    r = client.post(
        "/v1/ask",
        json={"question": "summarize", "files": [{"filename": "a.zip", "data": bad}]},
    )
    assert r.status_code == 422


def test_regenerate_reuses_stored_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    def fake_run_first(req, routing_question=None, owner=None, history=""):
        return AskResponse(answer="It's a PDF.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run_first)

    cid = _create(client)
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={
            "question": "summarize this",
            "files": [{"filename": "a.pdf", "data": _PDF_DATA}],
        },
    )

    captured_files: list = []

    def fake_run_regen(req, routing_question=None, owner=None):
        captured_files.append(req.files)
        return AskResponse(answer="Still a PDF.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run_regen)

    r = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert r.status_code == 200
    assert captured_files[-1][0].filename == "a.pdf"
