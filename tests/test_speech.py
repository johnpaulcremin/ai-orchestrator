"""Voice output: the TTS call and the POST /v1/speak endpoint (speaker-button
playback in the UI).
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from app import speech
from app.speech import SpeechError, speech_model, speech_voice, synthesize_speech


# --- speech_model / speech_voice ------------------------------------------------


def test_speech_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECH_MODEL", raising=False)
    assert speech_model() == "gpt-4o-mini-tts"


def test_speech_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECH_MODEL", "tts-1-hd")
    assert speech_model() == "tts-1-hd"


def test_speech_voice_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECH_VOICE", raising=False)
    assert speech_voice() == "alloy"


def test_speech_voice_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECH_VOICE", "nova")
    assert speech_voice() == "nova"


# --- synthesize_speech -----------------------------------------------------------


def test_synthesize_speech_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=b"fake mp3 bytes")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(speech, "get_client", lambda: fake_client)

    assert synthesize_speech("hello there") == b"fake mp3 bytes"
    assert captured["input"] == "hello there"
    assert captured["response_format"] == "mp3"


def test_synthesize_speech_uses_configured_model_and_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEECH_MODEL", "tts-1-hd")
    monkeypatch.setenv("SPEECH_VOICE", "nova")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=b"ok")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(speech, "get_client", lambda: fake_client)

    synthesize_speech("hi")
    assert captured["model"] == "tts-1-hd"
    assert captured["voice"] == "nova"


def test_synthesize_speech_truncates_long_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=b"ok")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(speech, "get_client", lambda: fake_client)

    synthesize_speech("x" * 10_000)
    assert len(captured["input"]) == 4000


def test_synthesize_speech_empty_text_raises() -> None:
    with pytest.raises(SpeechError):
        synthesize_speech("   ")


def test_synthesize_speech_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("OPENAI_API_KEY is not set.")

    monkeypatch.setattr(speech, "get_client", boom)
    with pytest.raises(SpeechError):
        synthesize_speech("hi")


def test_synthesize_speech_provider_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create(**_kw):
        raise RuntimeError("upstream boom")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(speech=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(speech, "get_client", lambda: fake_client)

    with pytest.raises(SpeechError):
        synthesize_speech("hi")


# --- HTTP integration: POST /v1/speak -------------------------------------------


def test_speak_endpoint_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.synthesize_speech", lambda text: b"fake mp3 bytes")
    r = client.post("/v1/speak", json={"text": "hello"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"fake mp3 bytes"


def test_speak_endpoint_provider_failure_is_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text):
        raise SpeechError("upstream boom")

    monkeypatch.setattr("app.main.synthesize_speech", boom)
    r = client.post("/v1/speak", json={"text": "hello"})
    assert r.status_code == 502
    assert "upstream boom" in r.json()["detail"]


def test_speak_endpoint_rejects_empty_text(client: TestClient) -> None:
    r = client.post("/v1/speak", json={"text": ""})
    assert r.status_code == 422


def test_speak_endpoint_rejects_oversized_text(client: TestClient) -> None:
    r = client.post("/v1/speak", json={"text": "x" * 60_000})
    assert r.status_code == 422


# --- HTTP integration: /v1/speak is subject to the daily budget cap -------------


def test_speak_endpoint_refused_when_budget_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text):
        raise AssertionError("must not synthesize speech once budget is refused")

    monkeypatch.setattr("app.main.synthesize_speech", boom)
    monkeypatch.setattr(
        "app.budget.reserve", lambda *a, **kw: ("Daily budget reached.", None)
    )

    r = client.post("/v1/speak", json={"text": "hello"})
    assert r.status_code == 402
    assert "Daily budget reached" in r.json()["detail"]


def test_speak_endpoint_records_spend_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.synthesize_speech", lambda text: b"fake mp3 bytes")
    recorded = {}
    monkeypatch.setattr(
        "app.main.record_spend",
        lambda owner, model, in_tok, out_tok, cost: recorded.update(
            owner=owner, model=model, cost=cost
        ),
    )

    r = client.post("/v1/speak", json={"text": "hello"})
    assert r.status_code == 200
    assert recorded["model"] == "gpt-4o-mini-tts"
    assert recorded["cost"] > 0
