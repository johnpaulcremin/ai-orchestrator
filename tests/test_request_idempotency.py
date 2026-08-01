"""Non-streaming idempotency (see app/request_registry.py and
_stream_and_persist's module docstring in app/routers/messages.py for the
full disconnect-proofing + idempotency rationale this is one half of):
ask/regenerate/edit/continue all dedup a repeated request_id to exactly one
model call, and POST /v1/requests/{request_id}/cancel is the explicit-abort
signal distinct from a bare disconnect.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.routers.messages
from app import request_registry
from app.schemas import AskRequest, AskResponse


@pytest.fixture(autouse=True)
def _clean_request_registry():
    request_registry._reset_for_tests()
    yield
    request_registry._reset_for_tests()


@pytest.fixture()
def orchestrator_calls(monkeypatch: pytest.MonkeyPatch) -> list[AskRequest]:
    """Replace run_orchestrator with a canned response; record every request."""
    calls: list[AskRequest] = []

    def fake_run_orchestrator(
        req: AskRequest,
        routing_question: str | None = None,
        owner: str | None = None,
        history: str = "",
        cacheable_system: str | None = None,
        anthropic_question: str | None = None,
        context_free: bool = False,
        pre_stage_timings: dict[str, int] | None = None,
        library_sources: list[dict] | None = None,
    ) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=f"canned:{len(calls)}", mode_used="auto->fast", notes="n"
        )

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)
    return calls


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


# --- non-streaming ask -------------------------------------------------------


def test_ask_duplicate_request_id_dispatches_one_call_and_same_result(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)

    first = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "request_id": "ask-dup-1"},
    )
    second = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "request_id": "ask-dup-1"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["answer"] == second.json()["answer"] == "canned:1"
    assert len(orchestrator_calls) == 1

    # Only ONE user turn + ONE assistant turn — the duplicate never
    # re-inserted the user's question either.
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assert len(messages) == 2


def test_ask_without_a_request_id_is_never_deduped(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    """Backward compatibility: an old client that never sends request_id
    gets today's behavior unchanged — every call is independent."""
    cid = _create(client)

    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})

    assert len(orchestrator_calls) == 2


def test_ask_different_request_ids_each_dispatch(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)

    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "request_id": "ask-a"},
    )
    client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "hi", "request_id": "ask-b"},
    )

    assert len(orchestrator_calls) == 2


# --- non-streaming regenerate -------------------------------------------------


def test_regenerate_duplicate_request_id_dispatches_one_call(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert len(orchestrator_calls) == 1

    first = client.post(
        f"/v1/conversations/{cid}/regenerate", json={"request_id": "regen-dup"}
    )
    second = client.post(
        f"/v1/conversations/{cid}/regenerate", json={"request_id": "regen-dup"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["answer"] == second.json()["answer"]
    # 1 for the ask + exactly 1 more for both regenerate calls combined.
    assert len(orchestrator_calls) == 2


# --- non-streaming edit --------------------------------------------------------


def test_edit_duplicate_request_id_dispatches_one_call(
    client: TestClient, orchestrator_calls: list[AskRequest]
) -> None:
    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert len(orchestrator_calls) == 1
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    user_message_id = next(m["id"] for m in messages if m["role"] == "user")

    body = {"question": "hi, edited", "request_id": "edit-dup"}
    first = client.post(
        f"/v1/conversations/{cid}/messages/{user_message_id}/edit", json=body
    )
    second = client.post(
        f"/v1/conversations/{cid}/messages/{user_message_id}/edit", json=body
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["answer"] == second.json()["answer"]
    assert len(orchestrator_calls) == 2


# --- non-streaming continue (query param, not a body field) -------------------


def test_continue_duplicate_request_id_dispatches_one_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[AskRequest] = []

    def fake_run_orchestrator(req: AskRequest, **kwargs: object) -> AskResponse:
        calls.append(req)
        return AskResponse(
            answer=" continued", mode_used="auto->fast", notes="n", truncated=False
        )

    monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake_run_orchestrator)

    cid = _create(client)
    client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    # /ask above also goes through the same (patched) run_orchestrator, so
    # `calls` already holds 1 entry before either /continue call.
    assert len(calls) == 1

    # Directly mark the assistant message truncated so /continue accepts it.
    from app import database

    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant_id = next(m["id"] for m in messages if m["role"] == "assistant")
    with database._connect() as conn:
        conn.execute("UPDATE messages SET truncated = 1 WHERE id = ?", (assistant_id,))

    first = client.post(
        f"/v1/conversations/{cid}/messages/{assistant_id}/continue"
        "?request_id=continue-dup"
    )
    second = client.post(
        f"/v1/conversations/{cid}/messages/{assistant_id}/continue"
        "?request_id=continue-dup"
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # +1 for the single deduped /continue call — the second arrival never
    # dispatched its own.
    assert len(calls) == 2


# --- explicit abort endpoint ---------------------------------------------------


def test_cancel_endpoint_marks_an_in_flight_entry() -> None:
    entry, is_new = request_registry.begin("cancel-me")
    assert is_new is True

    assert request_registry.mark_aborted("cancel-me") is True
    assert request_registry.is_aborted(entry) is True


def test_cancel_endpoint_returns_false_for_an_unknown_request_id(
    client: TestClient,
) -> None:
    res = client.post("/v1/requests/never-existed/cancel")
    assert res.status_code == 200
    assert res.json() == {"cancelled": False}


def test_cancel_endpoint_returns_false_for_an_already_finished_request_id(
    client: TestClient,
) -> None:
    entry, _ = request_registry.begin("already-done")
    request_registry.finish(entry, {"ok": True})

    res = client.post("/v1/requests/already-done/cancel")
    assert res.json() == {"cancelled": False}


def test_cancel_endpoint_marks_a_genuinely_in_flight_request_id(
    client: TestClient,
) -> None:
    request_registry.begin("still-running")

    res = client.post("/v1/requests/still-running/cancel")
    assert res.json() == {"cancelled": True}


# --- request_registry unit tests -----------------------------------------------


def test_begin_without_a_request_id_is_always_new() -> None:
    entry, is_new = request_registry.begin(None)
    assert entry is None
    assert is_new is True
    entry2, is_new2 = request_registry.begin("")
    assert entry2 is None
    assert is_new2 is True


def test_begin_the_same_id_twice_returns_the_same_entry_and_is_new_false() -> None:
    entry1, is_new1 = request_registry.begin("same-id")
    entry2, is_new2 = request_registry.begin("same-id")
    assert is_new1 is True
    assert is_new2 is False
    assert entry1 is entry2


def test_finish_wakes_a_waiting_duplicate_with_the_published_result() -> None:
    entry, _ = request_registry.begin("wake-me")
    request_registry.finish(entry, {"answer": "done"})

    entry2, is_new2 = request_registry.begin("wake-me")
    assert is_new2 is False
    assert request_registry.wait_for_result(entry2) == {"answer": "done"}


def test_wait_for_result_times_out_and_returns_none() -> None:
    entry, _ = request_registry.begin("never-finishes")
    assert request_registry.wait_for_result(entry, timeout=0.05) is None


def test_finish_is_a_no_op_for_an_untracked_none_entry() -> None:
    # Must not raise.
    request_registry.finish(None, {"answer": "ignored"})


def test_mark_aborted_returns_false_for_unknown_request_id() -> None:
    assert request_registry.mark_aborted("no-such-id") is False


def test_is_aborted_false_for_a_none_entry() -> None:
    assert request_registry.is_aborted(None) is False


def test_ttl_sweep_drops_a_stale_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_registry, "_TTL_SECONDS", 0)
    request_registry.begin("stale-one")
    # A sleep generous enough to clear monotonic-clock resolution on any
    # platform (Windows' default timer can be ~15ms coarse) — the point is
    # just "measurably later than created_at", not a tight bound.
    time.sleep(0.05)
    # begin() sweeps before checking for an existing entry (see its own
    # docstring/implementation) — with _TTL_SECONDS patched to 0, this call
    # sweeps the now-stale "stale-one" entry away, so it's treated as new
    # again rather than joining the (just-forgotten) old entry.
    _entry2, is_new2 = request_registry.begin("stale-one")
    assert is_new2 is True
