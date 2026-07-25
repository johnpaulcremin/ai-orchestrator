"""Actions/webhooks: the propose_action tool gating, extraction, the
propose-then-confirm cache-skip invariant, and end-to-end persistence + the
confirm/decline endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import actions, cache
from app.database import (
    add_message,
    claim_pending_action,
    create_conversation,
    get_message,
    set_action_status,
)
from app.orchestrator import (
    _extract_pending_action,
    run_orchestrator,
    stream_orchestrator,
)
from app.schemas import AskRequest, Mode


# --- actions.py: webhook_url / actions_enabled / post_webhook ----------------


def test_actions_enabled_requires_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIONS_WEBHOOK_URL", raising=False)
    assert actions.actions_enabled() is False
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    assert actions.actions_enabled() is True


def test_post_webhook_no_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACTIONS_WEBHOOK_URL", raising=False)
    success, detail = actions.post_webhook({"a": 1})
    assert success is False
    assert "No ACTIONS_WEBHOOK_URL" in detail


def test_post_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")

    def fake_post(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request)

    monkeypatch.setattr(actions.httpx, "post", fake_post)
    success, detail = actions.post_webhook({"a": 1})
    assert success is True
    assert "200" in detail


def test_post_webhook_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")

    def fake_post(url, json, timeout):
        request = httpx.Request("POST", url)
        return httpx.Response(500, request=request)

    monkeypatch.setattr(actions.httpx, "post", fake_post)
    success, detail = actions.post_webhook({"a": 1})
    assert success is False
    assert "500" in detail


def test_post_webhook_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")

    def fake_post(url, json, timeout):
        raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(actions.httpx, "post", fake_post)
    success, detail = actions.post_webhook({"a": 1})
    assert success is False
    assert "ConnectError" in detail


# --- orchestrator: _extract_pending_action ------------------------------------


def _fake_function_call(name: str, arguments: str) -> object:
    return SimpleNamespace(type="function_call", name=name, arguments=arguments)


def test_extract_pending_action_valid() -> None:
    result = SimpleNamespace(
        output=[
            _fake_function_call(
                "propose_action",
                json.dumps(
                    {
                        "action": "send_email",
                        "summary": "Email the report",
                        "payload": {"to": "a@example.com"},
                    }
                ),
            )
        ]
    )
    action = _extract_pending_action(result)
    assert action == {
        "action": "send_email",
        "summary": "Email the report",
        "payload": {"to": "a@example.com"},
    }


def test_extract_pending_action_ignores_other_function_calls() -> None:
    result = SimpleNamespace(
        output=[_fake_function_call("some_other_tool", json.dumps({"x": 1}))]
    )
    assert _extract_pending_action(result) is None


def test_extract_pending_action_malformed_json_tolerated() -> None:
    result = SimpleNamespace(
        output=[_fake_function_call("propose_action", "{not valid json")]
    )
    assert _extract_pending_action(result) is None


def test_extract_pending_action_missing_fields_tolerated() -> None:
    result = SimpleNamespace(
        output=[
            _fake_function_call("propose_action", json.dumps({"action": "send_email"}))
        ]
    )
    assert _extract_pending_action(result) is None


def test_extract_pending_action_no_output_attr() -> None:
    assert _extract_pending_action(SimpleNamespace()) is None


# --- orchestrator: gating + cache-skip + response wiring ----------------------


def test_run_orchestrator_passes_actions_true_only_when_enabled_and_openai(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}

    def fake_call_model(**kwargs):
        seen["actions"] = kwargs["actions"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="send an email to bob", mode=Mode.smart))
    assert seen["actions"] is True


def test_run_orchestrator_actions_false_when_not_configured(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACTIONS_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("actions", kwargs["actions"]) or "ok",
    )

    run_orchestrator(AskRequest(question="send an email to bob", mode=Mode.smart))
    assert seen["actions"] is False


def test_run_orchestrator_populates_pending_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["pending_action"].append(
            {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
        )
        return "I've drafted the email.\n\nConfirm below to run it."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(
        AskRequest(question="email bob the report", mode=Mode.smart)
    )
    assert result.pending_action is not None
    assert result.pending_action.action == "send_email"
    assert result.pending_action.summary == "Email Bob"
    assert result.pending_action.payload == {"to": "b"}


def test_run_orchestrator_skips_cache_when_pending_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["pending_action"].append(
            {"action": "send_email", "summary": "Email Bob", "payload": {}}
        )
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="email bob", mode=Mode.smart))

    key = cache.make_key("email bob", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_pending_action(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs):
        kwargs["pending_action"].append(
            {"action": "send_email", "summary": "Email Bob", "payload": {}}
        )
        yield "note"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(AskRequest(question="email bob", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["pending_action"] == {
        "action": "send_email",
        "summary": "Email Bob",
        "payload": {},
    }


def test_stream_orchestrator_omits_pending_action_key_when_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["hi"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))
    done = events[-1]
    assert "pending_action" not in done["data"]


# --- database: pending_action / action_status persistence + get/set ----------


def test_add_message_and_get_message_roundtrip_pending_action(db_path: Path) -> None:
    conv = create_conversation("t", None)
    encoded = json.dumps({"action": "send_email", "summary": "s", "payload": {}})
    row = add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="note",
        pending_action=encoded,
        action_status="pending",
    )
    fetched = get_message(int(row["id"]))
    assert fetched is not None
    assert json.loads(fetched["pending_action"]) == {
        "action": "send_email",
        "summary": "s",
        "payload": {},
    }
    assert fetched["action_status"] == "pending"


def test_get_message_missing_returns_none(db_path: Path) -> None:
    assert get_message(999999) is None


def test_set_action_status_updates_and_returns_row(db_path: Path) -> None:
    conv = create_conversation("t", None)
    row = add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="note",
        pending_action=json.dumps({"action": "a", "summary": "s", "payload": {}}),
        action_status="pending",
    )
    updated = set_action_status(int(row["id"]), "confirmed")
    assert updated is not None
    assert updated["action_status"] == "confirmed"


def test_set_action_status_missing_message_returns_none(db_path: Path) -> None:
    assert set_action_status(999999, "confirmed") is None


def test_claim_pending_action_wins_from_pending(db_path: Path) -> None:
    conv = create_conversation("t", None)
    row = add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="note",
        pending_action=json.dumps({"action": "a", "summary": "s", "payload": {}}),
        action_status="pending",
    )
    claimed = claim_pending_action(int(row["id"]), "confirmed")
    assert claimed is not None
    assert claimed["action_status"] == "confirmed"


def test_claim_pending_action_loses_when_already_resolved(db_path: Path) -> None:
    conv = create_conversation("t", None)
    row = add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="note",
        pending_action=json.dumps({"action": "a", "summary": "s", "payload": {}}),
        action_status="declined",
    )
    assert claim_pending_action(int(row["id"]), "confirmed") is None


def test_claim_pending_action_missing_message_returns_none(db_path: Path) -> None:
    assert claim_pending_action(999999, "confirmed") is None


# --- HTTP integration: persistence through ask / stream / regenerate ---------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_pending_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse, PendingAction

    def fake_run(req, routing_question=None, owner=None):
        return AskResponse(
            answer="note",
            mode_used="smart",
            notes="n",
            pending_action=PendingAction(
                action="send_email", summary="Email Bob", payload={"to": "b"}
            ),
        )

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "email bob"})

    assert r.status_code == 200
    assert r.json()["pending_action"] == {
        "action": "send_email",
        "summary": "Email Bob",
        "payload": {"to": "b"},
    }

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["pending_action"] == {
        "action": "send_email",
        "summary": "Email Bob",
        "payload": {"to": "b"},
    }
    assert assistant["action_status"] == "pending"


def test_stream_ask_persists_pending_action_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "note",
                "mode_used": "smart",
                "notes": "n",
                "pending_action": {
                    "action": "send_email",
                    "summary": "Email Bob",
                    "payload": {},
                },
            },
        }

    monkeypatch.setattr("app.main.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream", json={"question": "email bob"}
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["pending_action"]["action"] == "send_email"
    assert assistant["action_status"] == "pending"


# --- HTTP integration: the confirm/decline endpoint ---------------------------


def _assistant_message_with_pending_action(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """Create a conversation with an assistant message that has a pending action."""
    from app.schemas import AskResponse, PendingAction

    def fake_run(req, routing_question=None, owner=None):
        return AskResponse(
            answer="note",
            mode_used="smart",
            notes="n",
            pending_action=PendingAction(
                action="send_email", summary="Email Bob", payload={"to": "b"}
            ),
        )

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "email bob"})

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in messages if m["role"] == "assistant")
    return cid, int(assistant["id"])


def test_resolve_action_confirm_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACTIONS_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(
        "app.main.post_webhook", lambda payload: (True, "Webhook responded 200.")
    )
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)

    r = client.post(
        f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": True}
    )
    assert r.status_code == 200
    assert r.json()["action_status"] == "confirmed"


def test_resolve_action_confirm_webhook_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.main.post_webhook", lambda payload: (False, "Webhook responded 500.")
    )
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)

    r = client.post(
        f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": True}
    )
    assert r.status_code == 200
    assert r.json()["action_status"] == "failed"


def test_resolve_action_decline_never_calls_webhook(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(payload):
        raise AssertionError("webhook must not be called on decline")

    monkeypatch.setattr("app.main.post_webhook", boom)
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)

    r = client.post(
        f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": False}
    )
    assert r.status_code == 200
    assert r.json()["action_status"] == "declined"


def test_resolve_action_already_resolved_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.post_webhook", lambda payload: (True, "ok"))
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)

    r1 = client.post(
        f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": True}
    )
    assert r1.status_code == 200

    r2 = client.post(
        f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": True}
    )
    assert r2.status_code == 409


def test_resolve_action_message_not_found(client: TestClient) -> None:
    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/messages/999999/action", json={"confirm": True}
    )
    assert r.status_code == 404


def test_resolve_action_conversation_not_found(client: TestClient) -> None:
    r = client.post(
        "/v1/conversations/999999/messages/1/action", json={"confirm": True}
    )
    assert r.status_code == 404


def test_resolve_action_concurrent_confirms_fire_webhook_only_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent confirm requests must not both pass the pending-check
    and both post the webhook — only one may win the atomic claim.
    """
    import threading
    import time

    calls: list[int] = []
    call_lock = threading.Lock()

    def slow_webhook(payload):
        # Give the second thread a real window to race into the same
        # pending-check before the first thread's claim lands.
        time.sleep(0.05)
        with call_lock:
            calls.append(1)
        return True, "ok"

    monkeypatch.setattr("app.main.post_webhook", slow_webhook)
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)

    results: list[int] = []

    def fire():
        r = client.post(
            f"/v1/conversations/{cid}/messages/{mid}/action", json={"confirm": True}
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1  # the webhook fired exactly once
    assert sorted(results) == [200, 409]  # one winner, one conflict


def test_resolve_action_message_from_other_conversation_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.post_webhook", lambda payload: (True, "ok"))
    cid, mid = _assistant_message_with_pending_action(client, monkeypatch)
    other_cid = _create(client)

    r = client.post(
        f"/v1/conversations/{other_cid}/messages/{mid}/action", json={"confirm": True}
    )
    assert r.status_code == 404
