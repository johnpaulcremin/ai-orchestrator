"""Image input / vision: AskRequest.images validation, per-provider content
construction (OpenAI/Anthropic/LiteLLM), cache-skip, fallback threading, and
end-to-end persistence (including reuse on regenerate).
"""

from __future__ import annotations

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
from app.schemas import AskRequest, Mode

_PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


# --- schemas: AskRequest.images validation ------------------------------------


def test_ask_request_accepts_valid_data_url() -> None:
    req = AskRequest(question="what is this", images=[_PNG_1PX])
    assert req.images == [_PNG_1PX]


def test_ask_request_empty_images_list_becomes_none() -> None:
    req = AskRequest(question="hi", images=[])
    assert req.images is None


def test_ask_request_no_images_defaults_to_none() -> None:
    req = AskRequest(question="hi")
    assert req.images is None


def test_ask_request_rejects_too_many_images() -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="what is this", images=[_PNG_1PX] * 5)


def test_ask_request_accepts_max_images() -> None:
    req = AskRequest(question="what is this", images=[_PNG_1PX] * 4)
    assert req.images is not None
    assert len(req.images) == 4


def test_ask_request_rejects_bare_http_url() -> None:
    """Only data: URLs are accepted — a remote URL would have the PROVIDER
    fetch it server-side on our behalf, an SSRF vector via a third party."""
    with pytest.raises(ValidationError):
        AskRequest(question="what is this", images=["https://example.com/cat.png"])


def test_ask_request_rejects_javascript_url() -> None:
    with pytest.raises(ValidationError):
        AskRequest(question="what is this", images=["javascript:alert(1)"])


def test_ask_request_rejects_non_image_data_url() -> None:
    with pytest.raises(ValidationError):
        AskRequest(
            question="what is this",
            images=["data:text/plain;base64,aGVsbG8="],
        )


def test_ask_request_rejects_oversized_image() -> None:
    huge = "data:image/png;base64," + ("A" * 13_000_000)
    with pytest.raises(ValidationError):
        AskRequest(question="what is this", images=[huge])


# --- providers: _parse_data_url ------------------------------------------------


def test_parse_data_url_valid() -> None:
    assert providers._parse_data_url("data:image/png;base64,aGVsbG8=") == (
        "image/png",
        "aGVsbG8=",
    )


def test_parse_data_url_malformed_returns_none() -> None:
    assert providers._parse_data_url("not-a-data-url") is None
    assert providers._parse_data_url("data:image/png,missing-base64-marker") is None


# --- providers: _anthropic_content / _litellm_content -------------------------


def test_anthropic_content_no_attachments_returns_plain_string() -> None:
    assert providers._anthropic_content("hi", None) == "hi"
    assert providers._anthropic_content("hi", []) == "hi"


def test_anthropic_content_builds_text_and_image_blocks() -> None:
    content = providers._anthropic_content("what is this", [_PNG_1PX])
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"


def test_anthropic_content_skips_malformed_attachment() -> None:
    content = providers._anthropic_content("q", ["not-a-data-url", _PNG_1PX])
    # Only the valid attachment produced an image block (plus the text block).
    assert len(content) == 2


def test_litellm_content_no_attachments_returns_plain_string() -> None:
    assert providers._litellm_content("hi", None) == "hi"


def test_litellm_content_builds_text_and_image_url_blocks() -> None:
    content = providers._litellm_content("what is this", [_PNG_1PX])
    assert content[0] == {"type": "text", "text": "what is this"}
    assert content[1] == {"type": "image_url", "image_url": {"url": _PNG_1PX}}


# --- providers: call_anthropic / call_litellm actually forward attachments ---


def test_call_anthropic_forwards_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic(
        "claude-x", "what is this", 100, 30.0, attachments=[_PNG_1PX]
    )
    content = captured["messages"][0]["content"]
    assert content[1]["type"] == "image"


def test_call_litellm_forwards_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    providers.call_litellm(
        "gemini/gemini-2.5-pro", "what is this", 128, 30.0, attachments=[_PNG_1PX]
    )
    content = captured["messages"][0]["content"]
    assert content[1] == {"type": "image_url", "image_url": {"url": _PNG_1PX}}


def test_call_anthropic_no_attachments_sends_plain_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "hi", 100, 30.0)
    assert captured["messages"][0]["content"] == "hi"


# --- orchestrator: _build_input -----------------------------------------------


def test_build_input_no_attachments_returns_plain_string() -> None:
    assert _build_input("hi", None) == "hi"


def test_build_input_with_attachments_builds_multipart_content() -> None:
    result = _build_input("what is this", [_PNG_1PX])
    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this"},
                {"type": "input_image", "image_url": _PNG_1PX},
            ],
        }
    ]


# --- orchestrator: _cache_key skips caching when images are attached ---------


def test_cache_key_none_when_images_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    req = AskRequest(question="what is this", images=[_PNG_1PX])
    assert _cache_key(req) is None


def test_cache_key_present_without_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    req = AskRequest(question="what is this")
    assert _cache_key(req) is not None


# --- orchestrator: attachments threaded to _call_model on primary + fallback -


def test_run_orchestrator_threads_attachments_to_call_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("attachments", kwargs["attachments"]) or "ok",
    )

    orchestrator.run_orchestrator(
        AskRequest(question="what is this", mode=Mode.smart, images=[_PNG_1PX])
    )
    assert seen["attachments"] == [_PNG_1PX]


def test_run_orchestrator_fallback_also_gets_attachments(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike web_search/actions/generated-images, vision works across every
    provider, so — unlike those — attachments are kept on the fallback call."""
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
        seen["attachments"] = kwargs["attachments"]
        return "recovered"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(
        AskRequest(question="what is this", mode=Mode.smart, images=[_PNG_1PX])
    )
    assert result.answer == "recovered"
    assert seen["attachments"] == [_PNG_1PX]


def test_stream_orchestrator_threads_attachments(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}

    def fake_stream_model(**kwargs):
        seen["attachments"] = kwargs["attachments"]
        yield "ok"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    list(
        orchestrator.stream_orchestrator(
            AskRequest(question="what is this", mode=Mode.smart, images=[_PNG_1PX])
        )
    )
    assert seen["attachments"] == [_PNG_1PX]


def test_run_orchestrator_no_images_passes_none_attachments(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("attachments", kwargs["attachments"]) or "ok",
    )

    orchestrator.run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["attachments"] is None


# --- database: user-message images persist and round-trip ---------------------


def test_add_message_and_list_messages_roundtrip_user_images(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="user",
        content="what is this",
        images=json.dumps([_PNG_1PX]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["images"]) == [_PNG_1PX]


# --- HTTP integration: attachments persist + reach the orchestrator ----------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_user_attached_images(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    captured_images: list = []

    def fake_run(req, routing_question=None, owner=None, history=""):
        captured_images.append(req.images)
        return AskResponse(answer="It's a red square.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what is this", "images": [_PNG_1PX]},
    )
    assert r.status_code == 200

    # The orchestrator saw the attachment on the contextual request.
    assert captured_images[-1] == [_PNG_1PX]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message = next(m for m in persisted if m["role"] == "user")
    assert user_message["images"] == [_PNG_1PX]


def test_ask_endpoint_rejects_invalid_image_url(client: TestClient) -> None:
    r = client.post(
        "/v1/ask", json={"question": "what is this", "images": ["not-a-data-url"]}
    )
    assert r.status_code == 422


def test_regenerate_reuses_stored_images(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    def fake_run_first(req, routing_question=None, owner=None, history=""):
        return AskResponse(answer="It's a red square.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run_first)

    cid = _create(client)
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what is this", "images": [_PNG_1PX]},
    )

    captured_images: list = []

    def fake_run_regen(req, routing_question=None, owner=None):
        captured_images.append(req.images)
        return AskResponse(answer="Still a red square.", mode_used="smart", notes="n")

    monkeypatch.setattr("app.main.run_orchestrator", fake_run_regen)

    r = client.post(f"/v1/conversations/{cid}/regenerate", json={})
    assert r.status_code == 200
    assert captured_images[-1] == [_PNG_1PX]
