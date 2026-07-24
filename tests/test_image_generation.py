"""Image generation: the image_generation tool gating/config, extraction, cost
estimation, the cache-skip invariant, and end-to-end persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import cache
from app.database import add_message, create_conversation, list_messages
from app.orchestrator import (
    _build_image_generation_tool,
    _extract_images,
    _image_generation_enabled,
    _image_generation_model,
    _image_generation_quality,
    _image_generation_size,
    run_orchestrator,
    stream_orchestrator,
)
from app.schemas import AskRequest, Mode
from app.usage import estimate_image_cost


# --- usage.py: estimate_image_cost -------------------------------------------


def test_estimate_image_cost_zero_count_is_none() -> None:
    assert estimate_image_cost(0, "high") is None


def test_estimate_image_cost_uses_quality_default() -> None:
    assert estimate_image_cost(2, "low") == pytest.approx(0.04)


def test_estimate_image_cost_unknown_quality_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_COST_USD", raising=False)
    assert estimate_image_cost(1, "nonsense") == estimate_image_cost(1, "auto")


def test_estimate_image_cost_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_COST_USD", "0.5")
    assert estimate_image_cost(3, "high") == pytest.approx(1.5)


def test_estimate_image_cost_invalid_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_COST_USD", "not-a-number")
    assert estimate_image_cost(1, "high") == pytest.approx(0.19)


# --- orchestrator: config helpers ---------------------------------------------


def test_image_generation_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    assert _image_generation_enabled() is False
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    assert _image_generation_enabled() is True
    monkeypatch.setenv("IMAGE_GENERATION", "false")
    assert _image_generation_enabled() is False


def test_image_generation_model_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    assert _image_generation_model() == "gpt-image-1"
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1.5")
    assert _image_generation_model() == "gpt-image-1.5"


def test_image_generation_quality_default_is_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_QUALITY", raising=False)
    assert _image_generation_quality() == "high"


def test_image_generation_quality_invalid_falls_back_to_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "ultra")
    assert _image_generation_quality() == "high"


def test_image_generation_quality_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "low")
    assert _image_generation_quality() == "low"


def test_image_generation_size_default_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_SIZE", raising=False)
    assert _image_generation_size() == "auto"
    monkeypatch.setenv("IMAGE_GENERATION_SIZE", "1024x1536")
    assert _image_generation_size() == "1024x1536"


def test_build_image_generation_tool_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setenv("IMAGE_GENERATION_SIZE", "auto")
    assert _build_image_generation_tool() == {
        "type": "image_generation",
        "model": "gpt-image-1",
        "quality": "high",
        "size": "auto",
    }


# --- orchestrator: _extract_images --------------------------------------------


def _fake_image_call(status: str, result: str | None) -> object:
    return SimpleNamespace(type="image_generation_call", status=status, result=result)


def test_extract_images_completed_with_result() -> None:
    result = SimpleNamespace(output=[_fake_image_call("completed", "YmFzZTY0")])
    assert _extract_images(result) == ["data:image/png;base64,YmFzZTY0"]


def test_extract_images_ignores_non_completed_status() -> None:
    result = SimpleNamespace(output=[_fake_image_call("in_progress", "YmFzZTY0")])
    assert _extract_images(result) == []


def test_extract_images_ignores_missing_result() -> None:
    result = SimpleNamespace(output=[_fake_image_call("completed", None)])
    assert _extract_images(result) == []


def test_extract_images_ignores_other_output_items() -> None:
    result = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", status="completed", result=None)]
    )
    assert _extract_images(result) == []


def test_extract_images_no_output_attr() -> None:
    assert _extract_images(SimpleNamespace()) == []


def test_extract_images_multiple_calls() -> None:
    result = SimpleNamespace(
        output=[
            _fake_image_call("completed", "aaa"),
            _fake_image_call("completed", "bbb"),
        ]
    )
    assert _extract_images(result) == [
        "data:image/png;base64,aaa",
        "data:image/png;base64,bbb",
    ]


# --- orchestrator: gating + cost + cache-skip + response wiring --------------


def test_run_orchestrator_passes_images_true_only_when_enabled_and_openai(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}

    def fake_call_model(**kwargs):
        seen["images"] = kwargs["images"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["images"] is True


def test_run_orchestrator_images_false_when_not_configured(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("images", kwargs["images"]) or "ok",
    )

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["images"] is False


def test_run_orchestrator_populates_images_and_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        return "Here's the image you asked for."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert result.images == ["data:image/png;base64,aaa"]
    assert result.cost_usd == pytest.approx(0.19)


def test_run_orchestrator_skips_cache_when_images_generated(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))

    key = cache.make_key("draw a cat", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_images(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        yield "note"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["images"] == ["data:image/png;base64,aaa"]


def test_stream_orchestrator_omits_images_key_when_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["hi"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))
    done = events[-1]
    assert "images" not in done["data"]


# --- database: images persistence ---------------------------------------------


def test_add_message_and_list_messages_roundtrip_images(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="here you go",
        images=json.dumps(["data:image/png;base64,aaa"]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["images"]) == ["data:image/png;base64,aaa"]


def test_add_message_without_images_stores_null(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="assistant", content="hi")
    messages = list_messages(conv["id"])
    assert messages[0]["images"] is None


# --- HTTP integration: images persist through ask / stream --------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_images(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    def fake_run(req, routing_question=None, owner=None):
        return AskResponse(
            answer="Here's the image you asked for.",
            mode_used="smart",
            notes="n",
            images=["data:image/png;base64,aaa"],
        )

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "draw a cat"})

    assert r.status_code == 200
    assert r.json()["images"] == ["data:image/png;base64,aaa"]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["images"] == ["data:image/png;base64,aaa"]


def test_stream_ask_persists_images_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Here's the image you asked for.",
                "mode_used": "smart",
                "notes": "n",
                "images": ["data:image/png;base64,aaa"],
            },
        }

    monkeypatch.setattr("app.main.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream", json={"question": "draw a cat"}
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["images"] == ["data:image/png;base64,aaa"]
