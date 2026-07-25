"""Voice input: audio data-URL decoding, the transcription call, and the
POST /v1/transcribe endpoint (mic-button dictation in the UI).
"""

from __future__ import annotations

import base64
import types

import pytest
from fastapi.testclient import TestClient

from app import transcription
from app.transcription import (
    TranscriptionError,
    _decode_data_url,
    transcribe_audio,
    transcription_model,
)

_WEBM_DATA = "data:audio/webm;base64," + base64.b64encode(b"fake webm bytes").decode()


# --- _decode_data_url ----------------------------------------------------------


def test_decode_data_url_valid_webm() -> None:
    raw, extension = _decode_data_url(_WEBM_DATA)
    assert raw == b"fake webm bytes"
    assert extension == "webm"


@pytest.mark.parametrize(
    "mime,expected_ext",
    [
        ("audio/wav", "wav"),
        ("audio/mp3", "mp3"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "mp4"),
        ("audio/m4a", "m4a"),
        ("audio/ogg", "ogg"),
    ],
)
def test_decode_data_url_supported_mimes(mime: str, expected_ext: str) -> None:
    data_url = f"data:{mime};base64," + base64.b64encode(b"clip").decode()
    _, extension = _decode_data_url(data_url)
    assert extension == expected_ext


def test_decode_data_url_not_a_data_url() -> None:
    with pytest.raises(TranscriptionError):
        _decode_data_url("not-a-data-url")


def test_decode_data_url_unsupported_mime() -> None:
    bad = "data:audio/flac;base64," + base64.b64encode(b"x").decode()
    with pytest.raises(TranscriptionError):
        _decode_data_url(bad)


def test_decode_data_url_invalid_base64() -> None:
    with pytest.raises(TranscriptionError):
        _decode_data_url("data:audio/webm;base64,not-valid-base64!!!")


def test_decode_data_url_empty_audio() -> None:
    with pytest.raises(TranscriptionError):
        _decode_data_url("data:audio/webm;base64,")


# --- transcription_model -------------------------------------------------------


def test_transcription_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIPTION_MODEL", raising=False)
    assert transcription_model() == "gpt-4o-mini-transcribe"


def test_transcription_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "whisper-1")
    assert transcription_model() == "whisper-1"


# --- transcribe_audio ------------------------------------------------------------


def test_transcribe_audio_returns_stripped_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(text="  hello world  ")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(transcription, "get_client", lambda: fake_client)

    assert transcribe_audio(_WEBM_DATA) == "hello world"
    filename, raw, content_type = captured["file"]
    assert filename == "clip.webm"
    assert raw == b"fake webm bytes"
    assert content_type == "audio/webm"


def test_transcribe_audio_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIPTION_MODEL", "whisper-1")
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(text="ok")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(transcription, "get_client", lambda: fake_client)

    transcribe_audio(_WEBM_DATA)
    assert captured["model"] == "whisper-1"


def test_transcribe_audio_malformed_input_raises() -> None:
    with pytest.raises(TranscriptionError):
        transcribe_audio("not-a-data-url")


def test_transcribe_audio_no_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise RuntimeError("OPENAI_API_KEY is not set.")

    monkeypatch.setattr(transcription, "get_client", boom)
    with pytest.raises(TranscriptionError):
        transcribe_audio(_WEBM_DATA)


def test_transcribe_audio_provider_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def create(**_kw):
        raise RuntimeError("upstream boom")

    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(transcriptions=types.SimpleNamespace(create=create))
    )
    monkeypatch.setattr(transcription, "get_client", lambda: fake_client)

    with pytest.raises(TranscriptionError):
        transcribe_audio(_WEBM_DATA)


def test_transcribe_audio_missing_text_attr_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = types.SimpleNamespace(
        audio=types.SimpleNamespace(
            transcriptions=types.SimpleNamespace(
                create=lambda **_kw: types.SimpleNamespace()
            )
        )
    )
    monkeypatch.setattr(transcription, "get_client", lambda: fake_client)
    assert transcribe_audio(_WEBM_DATA) == ""


# --- HTTP integration: POST /v1/transcribe -------------------------------------


def test_transcribe_endpoint_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.transcribe_audio", lambda audio: "hello from the mic")
    r = client.post("/v1/transcribe", json={"audio": _WEBM_DATA})
    assert r.status_code == 200
    assert r.json() == {"text": "hello from the mic"}


def test_transcribe_endpoint_provider_failure_is_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(audio):
        raise TranscriptionError("upstream boom")

    monkeypatch.setattr("app.main.transcribe_audio", boom)
    r = client.post("/v1/transcribe", json={"audio": _WEBM_DATA})
    assert r.status_code == 502
    assert "upstream boom" in r.json()["detail"]


def test_transcribe_endpoint_rejects_malformed_audio(client: TestClient) -> None:
    r = client.post("/v1/transcribe", json={"audio": "not-a-data-url"})
    assert r.status_code == 422


def test_transcribe_endpoint_rejects_unsupported_mime(client: TestClient) -> None:
    bad = "data:audio/flac;base64," + base64.b64encode(b"x").decode()
    r = client.post("/v1/transcribe", json={"audio": bad})
    assert r.status_code == 422


def test_transcribe_endpoint_rejects_oversized_audio(client: TestClient) -> None:
    huge = "data:audio/webm;base64," + ("A" * 35_000_000)
    r = client.post("/v1/transcribe", json={"audio": huge})
    assert r.status_code == 422


# --- HTTP integration: /v1/transcribe is subject to the daily budget cap -------


def test_transcribe_endpoint_refused_when_budget_exhausted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(audio):
        raise AssertionError("must not transcribe once budget is refused")

    monkeypatch.setattr("app.main.transcribe_audio", boom)
    monkeypatch.setattr(
        "app.budget.reserve", lambda *a, **kw: ("Daily budget reached.", None)
    )

    r = client.post("/v1/transcribe", json={"audio": _WEBM_DATA})
    assert r.status_code == 402
    assert "Daily budget reached" in r.json()["detail"]


def test_transcribe_endpoint_records_spend_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.transcribe_audio", lambda audio: "hello from the mic")
    recorded = {}
    monkeypatch.setattr(
        "app.main.record_spend",
        lambda owner, model, in_tok, out_tok, cost: recorded.update(
            owner=owner, model=model, cost=cost
        ),
    )

    r = client.post("/v1/transcribe", json={"audio": _WEBM_DATA})
    assert r.status_code == 200
    assert recorded["model"] == "gpt-4o-mini-transcribe"
    assert recorded["cost"] > 0
