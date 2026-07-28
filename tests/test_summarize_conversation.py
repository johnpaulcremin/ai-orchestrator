"""On-demand conversation TL;DR: summarize_conversation_for_display (the
router-model call backing it) and POST /v1/conversations/{id}/summarize.

Distinct from context_summary.summarize_conversation, which folds older
turns into terse internal memory notes for the model — this produces a
short, human-readable recap for the user to actually read, and is never
persisted (re-clicking regenerates it fresh).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app.orchestrator import summarize_conversation_for_display


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


# --- summarize_conversation_for_display (router call) ------------------------


def test_summarize_conversation_for_display_empty_messages_is_empty() -> None:
    assert summarize_conversation_for_display([]) == ""


def test_summarize_conversation_for_display_messages_without_content_is_empty() -> None:
    assert summarize_conversation_for_display([{"role": "user", "content": ""}]) == ""


def test_summarize_conversation_for_display_without_client_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_client() -> object:
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr(orchestrator, "get_client", no_client)
    messages = [{"role": "user", "content": "hi"}]
    assert summarize_conversation_for_display(messages) == ""


class _FakeClient:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def with_options(self, **kwargs: object) -> "_FakeClient":
        self._captured["options"] = kwargs
        return self

    @property
    def responses(self) -> object:
        outer = self

        class _R:
            def create(self, **kwargs: object) -> object:
                outer._captured["input"] = kwargs.get("input")
                return type("Result", (), {"output_text": "the TL;DR"})()

        return _R()


def test_summarize_conversation_for_display_feeds_transcript_to_summarizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "get_client", lambda: _FakeClient(captured))

    messages = [
        {"role": "user", "content": "any good ramen spots?"},
        {"role": "assistant", "content": "Try Ichiran."},
    ]
    assert summarize_conversation_for_display(messages) == "the TL;DR"
    sent = str(captured["input"])
    assert "any good ramen spots?" in sent
    assert "Try Ichiran." in sent


def test_summarize_conversation_for_display_skips_content_less_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "get_client", lambda: _FakeClient(captured))

    messages = [
        {"role": "user", "content": "  "},
        {"role": "assistant", "content": "the real content"},
    ]
    summarize_conversation_for_display(messages)
    assert "the real content" in str(captured["input"])


def test_summarize_conversation_for_display_survives_summarizer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingClient:
        def with_options(self, **_kwargs: object) -> "_RaisingClient":
            return self

        @property
        def responses(self) -> object:
            class _R:
                def create(self, **_kwargs: object) -> object:
                    raise RuntimeError("boom")

            return _R()

    monkeypatch.setattr(orchestrator, "get_client", lambda: _RaisingClient())
    messages = [{"role": "user", "content": "hi"}]
    assert summarize_conversation_for_display(messages) == ""


# --- HTTP endpoint -------------------------------------------------------------


def test_summarize_endpoint_returns_the_summary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.main.summarize_conversation_for_display", lambda messages: "the TL;DR"
    )
    cid = _create(client)
    # Seed a message via import (no model call) so the conversation isn't empty.
    client.post(
        "/v1/conversations/import",
        json={
            "title": "t",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    imported_cid = client.get("/v1/conversations").json()[0]["id"]

    res = client.post(f"/v1/conversations/{imported_cid}/summarize")
    assert res.status_code == 200
    assert res.json() == {"summary": "the TL;DR"}
    del cid  # unused beyond existing-as-a-second-conversation


def test_summarize_endpoint_404_for_missing_conversation(client: TestClient) -> None:
    res = client.post("/v1/conversations/999999/summarize")
    assert res.status_code == 404


def test_summarize_endpoint_400_for_empty_conversation(client: TestClient) -> None:
    cid = _create(client)
    res = client.post(f"/v1/conversations/{cid}/summarize")
    assert res.status_code == 400


def test_summarize_endpoint_502_when_summarizer_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.main.summarize_conversation_for_display", lambda messages: ""
    )
    client.post(
        "/v1/conversations/import",
        json={"title": "t", "messages": [{"role": "user", "content": "hi"}]},
    )
    cid = client.get("/v1/conversations").json()[0]["id"]

    res = client.post(f"/v1/conversations/{cid}/summarize")
    assert res.status_code == 502


def test_summarize_endpoint_is_owned_by_the_caller(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JWT_SECRET", "summarize-secret")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "app.main.summarize_conversation_for_display", lambda messages: "the TL;DR"
    )

    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "password123"}
    )
    alice = client.post(
        "/v1/auth/login", json={"username": "alice", "password": "password123"}
    ).json()["access_token"]
    client.post(
        "/v1/auth/register", json={"username": "bob", "password": "password123"}
    )
    bob = client.post(
        "/v1/auth/login", json={"username": "bob", "password": "password123"}
    ).json()["access_token"]

    client.post(
        "/v1/conversations/import",
        json={"title": "alice's chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {alice}"},
    )
    cid = client.get(
        "/v1/conversations", headers={"Authorization": f"Bearer {alice}"}
    ).json()[0]["id"]

    assert (
        client.post(
            f"/v1/conversations/{cid}/summarize",
            headers={"Authorization": f"Bearer {bob}"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/conversations/{cid}/summarize",
            headers={"Authorization": f"Bearer {alice}"},
        ).status_code
        == 200
    )
