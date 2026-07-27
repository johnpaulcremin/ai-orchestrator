"""Truncation detection (provider layer) and the Continue action (DB + API).

A response is "truncated" when the provider stopped because it hit
max_output_tokens, not because it actually finished — OpenAI's Responses API
reports this as response.status == "incomplete", Anthropic as
message.stop_reason == "max_tokens", and LiteLLM/OpenAI-compat providers as
choices[0].finish_reason == "length". None of these were read before this
feature; each provider function now records it into an optional `truncated`
out-list, the same convention already used for `citations`/`pending_action`.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import orchestrator, providers
from app.database import append_to_message, create_conversation, get_message
from app.schemas import AskRequest, AskResponse, Mode


def _fake_openai_client() -> object:
    """A stand-in for get_client() whose .with_options() just returns itself —
    _call_openai calls that before ever touching _create_with_fallback (which
    these tests mock separately), so it must not blow up on a plain object()."""
    client = types.SimpleNamespace()
    client.with_options = lambda **_kw: client
    return client


# --- provider-level truncation detection --------------------------------------


def test_call_anthropic_flags_truncation_on_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create(**_kwargs: object) -> object:
        return types.SimpleNamespace(
            stop_reason="max_tokens",
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=20),
            content=[types.SimpleNamespace(type="text", text="cut off mid")],
        )

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=fake_create)
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda timeout: fake_client)

    truncated: list[bool] = []
    answer = providers.call_anthropic(
        "claude-sonnet-5", "hi", 100, 30.0, truncated=truncated
    )

    assert answer == "cut off mid"
    assert truncated == [True]


def test_call_anthropic_not_flagged_on_normal_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create(**_kwargs: object) -> object:
        return types.SimpleNamespace(
            stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=20),
            content=[types.SimpleNamespace(type="text", text="a full answer")],
        )

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=fake_create)
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda timeout: fake_client)

    truncated: list[bool] = []
    providers.call_anthropic("claude-sonnet-5", "hi", 100, 30.0, truncated=truncated)

    assert truncated == []


def test_call_litellm_flags_truncation_on_length_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_completion(**_kwargs: object) -> object:
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    finish_reason="length",
                    message=types.SimpleNamespace(content="cut off"),
                )
            ],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=10),
        )

    fake_litellm = types.SimpleNamespace(completion=fake_completion)
    monkeypatch.setattr(providers, "_litellm", lambda: fake_litellm)

    truncated: list[bool] = []
    answer = providers.call_litellm(
        "gemini/gemini-flash-latest", "hi", 100, 30.0, truncated=truncated
    )

    assert answer == "cut off"
    assert truncated == [True]


def test_call_litellm_not_flagged_on_stop_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_completion(**_kwargs: object) -> object:
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    finish_reason="stop",
                    message=types.SimpleNamespace(content="a full answer"),
                )
            ],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=10),
        )

    fake_litellm = types.SimpleNamespace(completion=fake_completion)
    monkeypatch.setattr(providers, "_litellm", lambda: fake_litellm)

    truncated: list[bool] = []
    providers.call_litellm(
        "gemini/gemini-flash-latest", "hi", 100, 30.0, truncated=truncated
    )

    assert truncated == []


def test_call_openai_flags_truncation_on_incomplete_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create_with_fallback(*_args: object, **_kwargs: object) -> object:
        return types.SimpleNamespace(status="incomplete", output_text="cut off mid")

    monkeypatch.setattr(
        orchestrator, "_create_with_fallback", fake_create_with_fallback
    )
    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai_client())

    truncated: list[bool] = []
    answer = orchestrator._call_openai("gpt-5", "hi", 100, truncated=truncated)

    assert answer == "cut off mid"
    assert truncated == [True]


def test_call_openai_not_flagged_when_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create_with_fallback(*_args: object, **_kwargs: object) -> object:
        return types.SimpleNamespace(status="completed", output_text="a full answer")

    monkeypatch.setattr(
        orchestrator, "_create_with_fallback", fake_create_with_fallback
    )
    monkeypatch.setattr(orchestrator, "get_client", lambda: _fake_openai_client())

    truncated: list[bool] = []
    orchestrator._call_openai("gpt-5", "hi", 100, truncated=truncated)

    assert truncated == []


# --- orchestrator-level plumbing: AskResponse.truncated -----------------------


def test_run_orchestrator_sets_truncated_from_call_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["truncated"].append(True)  # type: ignore[union-attr]
        return "cut off"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = orchestrator.run_orchestrator(AskRequest(question="hi", mode=Mode.fast))

    assert result.answer == "cut off"
    assert result.truncated is True


def test_run_orchestrator_defaults_truncated_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kwargs: "a full answer")

    result = orchestrator.run_orchestrator(AskRequest(question="hi", mode=Mode.fast))

    assert result.truncated is False


# --- database layer: append_to_message -----------------------------------------


def test_append_to_message_concatenates_and_sums_cost(db_path: Path) -> None:
    conversation = create_conversation("t")
    from app.database import add_message

    original = add_message(
        conversation["id"],
        "assistant",
        "cut off mid",
        input_tokens=100,
        output_tokens=200,
        cost_usd=0.05,
        truncated=True,
    )

    updated = append_to_message(
        conversation_id=conversation["id"],
        message_id=original["id"],
        additional_content="-way through",
        truncated=False,
        input_tokens=50,
        output_tokens=75,
        cost_usd=0.02,
    )

    assert updated is not None
    assert updated["content"] == "cut off mid-way through"
    assert updated["truncated"] == 0  # the continuation itself finished cleanly
    assert updated["input_tokens"] == 150
    assert updated["output_tokens"] == 275
    assert updated["cost_usd"] == pytest.approx(0.07)


def test_append_to_message_still_truncated_if_continuation_cuts_off_again(
    db_path: Path,
) -> None:
    conversation = create_conversation("t")
    from app.database import add_message

    original = add_message(conversation["id"], "assistant", "part one", truncated=True)

    updated = append_to_message(
        conversation_id=conversation["id"],
        message_id=original["id"],
        additional_content=" part two",
        truncated=True,
    )

    assert updated is not None
    assert updated["truncated"] == 1


def test_append_to_message_scoped_to_its_conversation(db_path: Path) -> None:
    from app.database import add_message

    conversation_a = create_conversation("a")
    conversation_b = create_conversation("b")
    message = add_message(conversation_a["id"], "assistant", "hi", truncated=True)

    result = append_to_message(
        conversation_id=conversation_b["id"],
        message_id=message["id"],
        additional_content=" more",
        truncated=False,
    )

    assert result is None
    assert get_message(message["id"])["content"] == "hi"  # untouched


def test_append_to_message_missing_message_returns_none(db_path: Path) -> None:
    conversation = create_conversation("t")
    result = append_to_message(
        conversation_id=conversation["id"],
        message_id=999999,
        additional_content="x",
        truncated=False,
    )
    assert result is None


# --- HTTP API: POST .../messages/{id}/continue ---------------------------------


def _create_conversation(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


@pytest.fixture()
def continue_orchestrator(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    """Canned continuation answer; records every run_orchestrator call."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer="-way through",
            mode_used="auto->fast",
            notes="continuation",
            truncated=False,
        )

    import app.main

    monkeypatch.setattr(app.main, "run_orchestrator", fake_run_orchestrator)
    return calls


def test_continue_appends_to_the_truncated_message(
    client: TestClient, continue_orchestrator: list[AskRequest]
) -> None:
    from app.database import add_message

    cid = _create_conversation(client)
    msg = add_message(cid, "assistant", "cut off mid", truncated=True)

    res = client.post(f"/v1/conversations/{cid}/messages/{msg['id']}/continue")

    assert res.status_code == 200
    body = res.json()
    assert body["content"] == "cut off mid-way through"
    assert body["truncated"] is False
    assert len(continue_orchestrator) == 1


def test_continue_404_for_missing_conversation(client: TestClient) -> None:
    res = client.post("/v1/conversations/999999/messages/1/continue")
    assert res.status_code == 404


def test_continue_404_for_missing_message(client: TestClient) -> None:
    cid = _create_conversation(client)
    res = client.post(f"/v1/conversations/{cid}/messages/999999/continue")
    assert res.status_code == 404


def test_continue_400_for_a_user_message(client: TestClient) -> None:
    from app.database import add_message

    cid = _create_conversation(client)
    msg = add_message(cid, "user", "hello")

    res = client.post(f"/v1/conversations/{cid}/messages/{msg['id']}/continue")
    assert res.status_code == 400


def test_continue_400_for_a_message_that_was_not_truncated(
    client: TestClient,
) -> None:
    from app.database import add_message

    cid = _create_conversation(client)
    msg = add_message(cid, "assistant", "a complete answer", truncated=False)

    res = client.post(f"/v1/conversations/{cid}/messages/{msg['id']}/continue")
    assert res.status_code == 400


def test_continue_scoped_to_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main
    from app.database import add_message

    monkeypatch.setenv("JWT_SECRET", "x" * 32)

    def register_login(username: str) -> str:
        client.post(
            "/v1/auth/register", json={"username": username, "password": "pw123456"}
        )
        res = client.post(
            "/v1/auth/login", json={"username": username, "password": "pw123456"}
        )
        return str(res.json()["access_token"])

    alice_token = register_login("alice")
    bob_token = register_login("bob")

    cid = int(
        client.post(
            "/v1/conversations",
            json={"title": "t"},
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()["id"]
    )
    msg = add_message(cid, "assistant", "cut off", truncated=True)

    monkeypatch.setattr(
        app.main,
        "run_orchestrator",
        lambda req, routing_question=None, owner=None, history="": AskResponse(
            answer="more", mode_used="auto->fast", notes="n"
        ),
    )

    res = client.post(
        f"/v1/conversations/{cid}/messages/{msg['id']}/continue",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert res.status_code == 404
