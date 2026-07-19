from __future__ import annotations

import types

import httpx
import pytest
from openai import BadRequestError

from app import orchestrator, providers


def _fake_openai(create_fn):
    responses = types.SimpleNamespace(create=create_fn)
    client = types.SimpleNamespace(responses=responses)
    client.with_options = lambda **_kw: client
    return client


def _bad_request() -> BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return BadRequestError(
        "bad", response=httpx.Response(400, request=request), body=None
    )


# --- OpenAI non-streaming ---------------------------------------------------


def test_call_openai_passes_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_text="ANSWER")

    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai(create))

    out = orchestrator._call_openai("gpt-5", "q", 100, "low")
    assert out == "ANSWER"
    assert calls[0]["reasoning"] == {"effort": "low"}


def test_call_openai_retries_without_reasoning_on_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "reasoning" in kwargs:
            raise _bad_request()
        return types.SimpleNamespace(output_text="OK")

    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai(create))

    out = orchestrator._call_openai("gpt-5", "q", 100, "high")
    assert out == "OK"
    assert len(calls) == 2
    assert "reasoning" in calls[0] and "reasoning" not in calls[1]


# --- OpenAI streaming -------------------------------------------------------


def _event(type_: str, **kw):
    return types.SimpleNamespace(type=type_, **kw)


def test_stream_openai_yields_text_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="Hel"),
                _event("response.output_text.delta", delta="lo"),
                _event("response.completed"),
            ]
        )

    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai(create))
    assert list(orchestrator._stream_openai("gpt-5", "q", 100)) == ["Hel", "lo"]


def test_stream_openai_raises_on_failure_event(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="partial"),
                _event(
                    "response.failed",
                    response=types.SimpleNamespace(
                        error=types.SimpleNamespace(message="boom")
                    ),
                ),
            ]
        )

    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai(create))
    gen = orchestrator._stream_openai("gpt-5", "q", 100)
    assert next(gen) == "partial"
    with pytest.raises(orchestrator._ModelStreamError):
        next(gen)


# --- timeout parsing --------------------------------------------------------


def test_timeout_seconds_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "30")
    assert orchestrator._timeout_seconds() == 30.0
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "abc")
    assert orchestrator._timeout_seconds() == 120.0
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "-5")
    assert orchestrator._timeout_seconds() == 120.0
    monkeypatch.delenv("OPENAI_TIMEOUT_SECONDS", raising=False)
    assert orchestrator._timeout_seconds() == 120.0


# --- Anthropic provider -----------------------------------------------------


def test_anthropic_model_strips_prefix() -> None:
    assert providers._anthropic_model("anthropic/claude-opus-4-8") == "claude-opus-4-8"
    assert providers._anthropic_model("claude-sonnet-5") == "claude-sonnet-5"


def test_anthropic_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_anthropic_client", None)
    with pytest.raises(RuntimeError):
        providers.anthropic_client(30.0)


def test_call_anthropic_joins_only_text_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="Hello "),
            types.SimpleNamespace(type="tool_use", text="IGNORED"),
            types.SimpleNamespace(type="text", text="world"),
        ]
    )
    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **_kw: message)
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    assert providers.call_anthropic("claude-x", "q", 100, 30.0) == "Hello world"


def test_stream_anthropic_yields_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        text_stream = ["a", "b", ""]

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **_kw: FakeStream())
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    assert list(providers.stream_anthropic("claude-x", "q", 100, 30.0)) == ["a", "b"]


# --- LiteLLM provider (Gemini / Bedrock / Mistral / ...) --------------------


def test_call_litellm_passes_args_and_extracts_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="hi from gemini")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    out = providers.call_litellm("gemini/gemini-2.5-pro", "q", 128, 30.0, "low")
    assert out == "hi from gemini"
    assert captured["model"] == "gemini/gemini-2.5-pro"
    assert captured["max_tokens"] == 128
    assert captured["reasoning_effort"] == "low"
    assert captured["messages"] == [{"role": "user", "content": "q"}]


def test_call_litellm_omits_reasoning_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    providers.call_litellm("mistral/mistral-large-latest", "q", 128, 30.0, "")
    assert "reasoning_effort" not in captured


def test_stream_litellm_yields_delta_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def chunk(content):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(delta=types.SimpleNamespace(content=content))
            ]
        )

    def completion(**_kwargs):
        return iter([chunk("Hel"), chunk("lo"), chunk(None)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    assert list(providers.stream_litellm("bedrock/x", "q", 128, 30.0)) == ["Hel", "lo"]
