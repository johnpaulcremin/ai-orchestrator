"""The output-token ceiling an answer was generated under: recorded, carried,
persisted, and described.

A truncated answer used to say only THAT it was cut off. The number it was cut
off at existed on the routing decision and was thrown away, so the truncation
notice couldn't name it and the re-route control couldn't tell which of its
options had any more headroom — it listed "fast tier" (1,500) beside "smart
tier" (4,000) as if either were a remedy for an answer that had just hit 4,000.

The ceiling is a fact about the ATTEMPT, which is why it's persisted per message
rather than re-derived later from mode_used plus today's environment. Re-deriving
would be wrong twice over: the caps are runtime-configurable, and "forced:<model>"
never says which tier's budget it borrowed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.messages as messages_module
from app import orchestrator
from app.database import (
    add_message,
    append_to_message,
    branch_conversation,
    create_conversation,
    duplicate_conversation,
    get_message,
)
from app.routing import Mode, decide_route, tier_output_caps
from app.schemas import AskRequest, AskResponse

# --- tier_output_caps: one source for the three numbers -------------------------


def test_tier_output_caps_reports_the_documented_defaults() -> None:
    assert tier_output_caps() == {"budget": 800, "fast": 1500, "smart": 4000}


@pytest.mark.parametrize(
    "tier,env",
    [
        ("budget", "BUDGET_MAX_OUTPUT_TOKENS"),
        ("fast", "FAST_MAX_OUTPUT_TOKENS"),
        ("smart", "SMART_MAX_OUTPUT_TOKENS"),
    ],
)
def test_tier_output_caps_follows_its_env_var(
    monkeypatch: pytest.MonkeyPatch, tier: str, env: str
) -> None:
    monkeypatch.setenv(env, "1234")
    assert tier_output_caps()[tier] == 1234


@pytest.mark.parametrize(
    "mode,env",
    [
        (Mode.budget, "BUDGET_MAX_OUTPUT_TOKENS"),
        (Mode.fast, "FAST_MAX_OUTPUT_TOKENS"),
        (Mode.smart, "SMART_MAX_OUTPUT_TOKENS"),
    ],
)
def test_a_routing_decision_uses_the_same_numbers(
    monkeypatch: pytest.MonkeyPatch, mode: Mode, env: str
) -> None:
    """The point of the helper: the UI's numbers and the router's numbers can't
    drift, because there is only one place that reads these env vars."""
    monkeypatch.setenv("OPENAI_MODEL_BUDGET", "budget-x")  # so budget is its own tier
    monkeypatch.setenv(env, "999")

    assert decide_route("q", mode).max_output_tokens == 999


def test_a_forced_model_borrows_the_smart_ceiling() -> None:
    """Why the ceiling can't be recovered from mode_used later: this records
    "forced:x" and says nothing about which tier's budget it took."""
    decision = decide_route("q", Mode.auto, forced_model="x")

    assert decision.mode_used == "forced:x"
    assert decision.max_output_tokens == tier_output_caps()["smart"]


# --- the orchestrator puts it on the response ----------------------------------


def test_run_orchestrator_reports_the_ceiling_it_answered_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_k: "an answer")

    result = orchestrator.run_orchestrator(AskRequest(question="q", mode=Mode.fast))

    assert result.max_output_tokens == tier_output_caps()["fast"]


def test_stream_orchestrator_reports_the_ceiling_in_its_done_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream(**_kwargs: object):
        yield "an answer"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream)

    events = list(
        orchestrator.stream_orchestrator(AskRequest(question="q", mode=Mode.smart))
    )

    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["max_output_tokens"] == tier_output_caps()["smart"]


# --- persistence, on every answer path -----------------------------------------


def _assistant_row(client: TestClient, cid: int) -> dict[str, Any]:
    messages = client.get(f"/v1/conversations/{cid}/messages").json()
    return next(m for m in reversed(messages) if m["role"] == "assistant")


def _truncated_at(cap: int | None) -> AskResponse:
    return AskResponse(
        answer="cut off mid",
        mode_used="auto->smart:analysis",
        notes="n",
        model="gpt-5",
        truncated=True,
        max_output_tokens=cap,
    )


def test_ask_persists_the_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _truncated_at(4000)
    )
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])

    body = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "q", "mode": "auto"}
    ).json()

    assert body["max_output_tokens"] == 4000
    assert _assistant_row(client, cid)["max_output_tokens"] == 4000


def test_regenerate_persists_the_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry paths go through the same persister (see _shared.py), so this
    is the check that the shared one carries the column rather than one path
    quietly omitting it — the exact bug class that lost `workflow_steps`."""
    monkeypatch.setattr(
        messages_module, "run_orchestrator", lambda req, **k: _truncated_at(1500)
    )
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    add_message(cid, "user", "q")
    add_message(cid, "assistant", "old", truncated=True, max_output_tokens=4000)

    client.post(f"/v1/conversations/{cid}/regenerate", json={})

    assert _assistant_row(client, cid)["max_output_tokens"] == 1500


def test_a_workflow_answer_records_no_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow has no single ceiling — each step has its own — so it records
    None rather than a number that would describe only one of them. This is also
    what makes a workflow retry a real remedy for a truncated answer."""
    result = AskResponse(answer="stepwise", mode_used="workflow(3 steps)", notes="n")
    monkeypatch.setattr(messages_module, "run_workflow", lambda req, **k: result)
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])

    client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "q", "mode": "workflow"}
    )

    assert _assistant_row(client, cid)["max_output_tokens"] is None


# --- Continue: the ceiling describes the LATEST attempt ------------------------


def test_append_to_message_replaces_the_ceiling(db_path: Path) -> None:
    cid = int(create_conversation("t")["id"])
    msg = add_message(
        cid, "assistant", "part one", truncated=True, max_output_tokens=800
    )

    append_to_message(cid, int(msg["id"]), " part two", True, max_output_tokens=4000)

    # Replaced, not summed: `truncated` on the row now describes the
    # continuation's own outcome, so the ceiling beside it has to describe the
    # same attempt or the notice would name a limit some earlier attempt hit.
    assert get_message(int(msg["id"]))["max_output_tokens"] == 4000


def test_append_to_message_keeps_the_ceiling_when_not_given(db_path: Path) -> None:
    cid = int(create_conversation("t")["id"])
    msg = add_message(
        cid, "assistant", "part one", truncated=True, max_output_tokens=800
    )

    append_to_message(cid, int(msg["id"]), " part two", False)

    assert get_message(int(msg["id"]))["max_output_tokens"] == 800


def test_continue_records_the_continuation_s_own_ceiling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        messages_module,
        "run_orchestrator",
        lambda req, **k: AskResponse(
            answer=" and the rest",
            mode_used="smart",
            notes="n",
            truncated=True,
            max_output_tokens=4000,
        ),
    )
    cid = int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])
    msg = add_message(
        cid,
        "assistant",
        "cut off",
        mode_used="auto->budget",
        truncated=True,
        max_output_tokens=800,
    )

    res = client.post(f"/v1/conversations/{cid}/messages/{msg['id']}/continue")

    assert res.status_code == 200
    assert res.json()["max_output_tokens"] == 4000


# --- round-trips: copy, branch, export/import, undo ----------------------------


def test_duplicate_carries_the_ceiling(db_path: Path) -> None:
    cid = int(create_conversation("t")["id"])
    add_message(cid, "assistant", "cut off", truncated=True, max_output_tokens=1500)

    copy_id = int(duplicate_conversation(cid, None)["id"])

    from app.database import list_messages

    assert list_messages(copy_id)[0]["max_output_tokens"] == 1500


def test_branch_carries_the_ceiling(db_path: Path) -> None:
    cid = int(create_conversation("t")["id"])
    add_message(cid, "user", "q")
    msg = add_message(
        cid, "assistant", "cut off", truncated=True, max_output_tokens=1500
    )

    branch_id = int(branch_conversation(cid, None, int(msg["id"]))["id"])

    from app.database import list_messages

    assert list_messages(branch_id)[-1]["max_output_tokens"] == 1500


def test_import_and_restore_carry_the_ceiling(client: TestClient) -> None:
    """Both hand-written column lists outside the shared persister. Every one of
    these that drops the field turns a truncated answer's own ceiling into
    "unknown" on a reload, which reads exactly like an answer that never had one."""
    imported = client.post(
        "/v1/conversations/import",
        json={
            "title": "t",
            "messages": [
                {
                    "role": "assistant",
                    "content": "cut off",
                    "truncated": True,
                    "max_output_tokens": 1500,
                }
            ],
        },
    )
    assert imported.status_code == 200
    cid = int(imported.json()["id"])
    assert _assistant_row(client, cid)["max_output_tokens"] == 1500

    restored = client.post(
        f"/v1/conversations/{cid}/messages/restore",
        json={
            "role": "assistant",
            "content": "cut off again",
            "truncated": True,
            "max_output_tokens": 800,
        },
    )
    assert restored.status_code == 200
    assert restored.json()["max_output_tokens"] == 800


def test_import_rejects_a_negative_ceiling(client: TestClient) -> None:
    res = client.post(
        "/v1/conversations/import",
        json={
            "title": "t",
            "messages": [
                {"role": "assistant", "content": "c", "max_output_tokens": -1}
            ],
        },
    )

    assert res.status_code == 422


# --- /v1/status: the caps the UI describes options with ------------------------


def test_status_exposes_the_tier_ceilings(client: TestClient) -> None:
    caps = client.get("/v1/status").json()["output_token_caps"]

    assert caps == tier_output_caps()
    assert caps["budget"] < caps["fast"] < caps["smart"]
