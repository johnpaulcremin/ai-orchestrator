"""OpenAI /v1/chat/completions compatibility (POST /v1/chat/completions):
lets any OpenAI-SDK-shaped client point at this app. Stateless like
/v1/ask — nothing here is persisted as a conversation.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.routers.compat
from app.schemas import AskRequest, AskResponse


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer="hello there",
            mode_used=f"forced:{req.mode.value}",
            notes="n",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
        )

    monkeypatch.setattr(app.routers.compat, "run_orchestrator", fake_run_orchestrator)
    return calls


def test_basic_completion_returns_openai_shaped_response(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi there"}]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello there",
    }
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }
    assert body["id"].startswith("chatcmpl-")
    assert orchestrator_calls[0].question == "hi there"


def test_truncated_answer_reports_length_finish_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_orchestrator(req: AskRequest, **_kwargs: object) -> AskResponse:
        return AskResponse(
            answer="cut off", mode_used="fast", notes="n", truncated=True
        )

    monkeypatch.setattr(app.routers.compat, "run_orchestrator", fake_run_orchestrator)

    res = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert res.json()["choices"][0]["finish_reason"] == "length"


@pytest.mark.parametrize("mode_keyword", ["auto", "budget", "fast", "smart"])
def test_a_mode_keyword_as_model_selects_that_routing_mode(
    client: TestClient, orchestrator_calls: list[AskRequest], mode_keyword: str
) -> None:
    client.post(
        "/v1/chat/completions",
        json={"model": mode_keyword, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert orchestrator_calls[0].mode.value == mode_keyword
    assert orchestrator_calls[0].model is None


def test_a_non_mode_model_name_forces_that_exact_model(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert orchestrator_calls[0].mode.value == "smart"
    assert orchestrator_calls[0].model == "gpt-5"


def test_an_invalid_forced_model_name_is_a_400(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={
            "model": "not a valid model!",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert res.status_code == 400


def test_system_and_history_messages_are_folded_into_the_context(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "system", "content": "Always answer in French."},
                {"role": "user", "content": "earlier turn"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "the real question"},
            ]
        },
    )
    sent = orchestrator_calls[0].question
    assert "Always answer in French." in sent
    assert "earlier turn" in sent
    assert "earlier answer" in sent
    assert sent.endswith("the real question")


def test_last_message_must_be_from_the_user(client: TestClient) -> None:
    res = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "assistant", "content": "not a question"}]},
    )
    assert res.status_code == 422


def test_a_single_user_message_is_context_free(
    client: TestClient,
    orchestrator_calls: list[AskRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        captured.update(kwargs)
        return AskResponse(answer="a", mode_used="fast", notes="n")

    monkeypatch.setattr(app.routers.compat, "run_orchestrator", fake_run_orchestrator)

    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert captured["context_free"] is True


def test_prior_history_is_not_context_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        captured.update(kwargs)
        return AskResponse(answer="a", mode_used="fast", notes="n")

    monkeypatch.setattr(app.routers.compat, "run_orchestrator", fake_run_orchestrator)

    client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "follow-up"},
            ]
        },
    )
    assert captured["context_free"] is False


def test_streaming_yields_openai_shaped_chunks_then_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req: AskRequest, routing_question=None, owner=None, **_kwargs):
        yield {"event": "meta", "data": {"mode_used": "auto->fast", "model": "m"}}
        yield {"event": "delta", "data": {"text": "Hel"}}
        yield {"event": "delta", "data": {"text": "lo"}}
        yield {"event": "done", "data": {"answer": "Hello", "truncated": False}}

    monkeypatch.setattr(app.routers.compat, "stream_orchestrator", fake_stream)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as res:
        assert res.status_code == 200
        body = "".join(res.iter_text())

    lines = [line for line in body.split("\n\n") if line.strip()]
    payloads = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert lines[-1] == "data: [DONE]"

    assert payloads[0]["object"] == "chat.completion.chunk"
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    contents = [
        p["choices"][0]["delta"].get("content")
        for p in payloads
        if "content" in p["choices"][0]["delta"]
    ]
    assert "".join(contents) == "Hello"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["choices"][0]["delta"] == {}


def test_streaming_reports_length_on_a_truncated_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req: AskRequest, routing_question=None, owner=None, **_kwargs):
        yield {"event": "delta", "data": {"text": "cut"}}
        yield {"event": "done", "data": {"answer": "cut", "truncated": True}}

    monkeypatch.setattr(app.routers.compat, "stream_orchestrator", fake_stream)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as res:
        body = "".join(res.iter_text())

    lines = [line for line in body.split("\n\n") if line.strip()]
    payloads = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
    assert payloads[-1]["choices"][0]["finish_reason"] == "length"
