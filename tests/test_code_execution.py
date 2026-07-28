"""Code execution: the code_interpreter tool gating/config, output extraction,
cost estimation, the cache-skip invariant, and end-to-end persistence.

The tool runs in OpenAI's own sandboxed container, never on this machine —
same trust boundary as web_search/image_generation, and wired through the
exact same "OpenAI-only, opt-in, offered-not-forced" pattern.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import cache
from app.orchestrator import (
    _build_tools,
    _code_execution_enabled,
    _code_execution_note,
    _extract_code_results,
    run_orchestrator,
    stream_orchestrator,
)
from app.schemas import AskRequest, Mode
from app.usage import estimate_code_execution_cost


# --- usage.py: estimate_code_execution_cost ------------------------------------


def test_estimate_code_execution_cost_zero_count_is_none() -> None:
    assert estimate_code_execution_cost(0) is None


def test_estimate_code_execution_cost_default_rate() -> None:
    assert estimate_code_execution_cost(2) == pytest.approx(0.06)


def test_estimate_code_execution_cost_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION_COST_USD", "0.5")
    assert estimate_code_execution_cost(3) == pytest.approx(1.5)


def test_estimate_code_execution_cost_invalid_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION_COST_USD", "not-a-number")
    assert estimate_code_execution_cost(1) == pytest.approx(0.03)


# --- orchestrator: config helper -----------------------------------------------


def test_code_execution_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_EXECUTION", raising=False)
    assert _code_execution_enabled() is False
    monkeypatch.setenv("CODE_EXECUTION", "true")
    assert _code_execution_enabled() is True
    monkeypatch.setenv("CODE_EXECUTION", "false")
    assert _code_execution_enabled() is False


# --- orchestrator: _build_tools includes code_interpreter ----------------------


def test_build_tools_includes_code_interpreter_only_when_requested() -> None:
    without = _build_tools(False, False, False, False)
    assert without == {}
    with_code = _build_tools(False, False, False, True)
    assert {"type": "code_interpreter", "container": {"type": "auto"}} in with_code[
        "tools"
    ]


# --- orchestrator: _extract_code_results ---------------------------------------


def _fake_code_call(
    status: str, code: str | None, outputs: list[object] | None = None
) -> object:
    return SimpleNamespace(
        type="code_interpreter_call", status=status, code=code, outputs=outputs or []
    )


def test_extract_code_results_completed_with_logs() -> None:
    call = _fake_code_call(
        "completed",
        "print(2 + 2)",
        [SimpleNamespace(type="logs", logs="4")],
    )
    result = SimpleNamespace(output=[call])
    assert _extract_code_results(result) == [
        {"code": "print(2 + 2)", "logs": "4", "images": []}
    ]


def test_extract_code_results_completed_with_image_output() -> None:
    call = _fake_code_call(
        "completed",
        "plot()",
        [SimpleNamespace(type="image", url="https://example.com/plot.png")],
    )
    result = SimpleNamespace(output=[call])
    extracted = _extract_code_results(result)
    assert extracted == [
        {"code": "plot()", "logs": None, "images": ["https://example.com/plot.png"]}
    ]


def test_extract_code_results_ignores_non_completed_status() -> None:
    call = _fake_code_call("in_progress", "print(1)")
    result = SimpleNamespace(output=[call])
    assert _extract_code_results(result) == []


def test_extract_code_results_ignores_missing_code() -> None:
    call = _fake_code_call("completed", None)
    result = SimpleNamespace(output=[call])
    assert _extract_code_results(result) == []


def test_extract_code_results_ignores_other_output_items() -> None:
    result = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", status="completed")]
    )
    assert _extract_code_results(result) == []


def test_extract_code_results_no_output_attr() -> None:
    assert _extract_code_results(SimpleNamespace()) == []


def test_extract_code_results_multiple_calls() -> None:
    result = SimpleNamespace(
        output=[
            _fake_code_call(
                "completed", "a = 1", [SimpleNamespace(type="logs", logs="")]
            ),
            _fake_code_call(
                "completed", "b = 2", [SimpleNamespace(type="logs", logs="ok")]
            ),
        ]
    )
    extracted = _extract_code_results(result)
    assert [r["code"] for r in extracted] == ["a = 1", "b = 2"]


# --- orchestrator: note text ----------------------------------------------------


def test_code_execution_note_singular_and_plural() -> None:
    assert "a snippet" in _code_execution_note(1)
    assert "1 snippet" not in _code_execution_note(1)
    assert "2 snippets" in _code_execution_note(2)


# --- orchestrator: gating + cost + cache-skip + response wiring ---------------


def test_run_orchestrator_passes_code_execution_true_only_when_enabled_and_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["code_execution"] = kwargs["code_execution"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["code_execution"] is True


def test_run_orchestrator_code_execution_false_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODE_EXECUTION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["code_execution"] = kwargs["code_execution"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["code_execution"] is False


def test_run_orchestrator_code_execution_never_reaches_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # call_anthropic's signature has no code_execution param at all — the tool
    # is structurally OpenAI-only. This just confirms CODE_EXECUTION=true
    # doesn't break routing to a Claude-served tier (no crash, normal answer).
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "call_anthropic", lambda *a, **k: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.answer == "ok"
    assert result.code_results is None


def test_run_orchestrator_returns_and_prices_code_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["code_results"].append(  # type: ignore[union-attr]
            {"code": "print(1)", "logs": "1", "images": []}
        )
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))

    assert result.code_results is not None
    assert result.code_results[0].code == "print(1)"
    assert result.code_results[0].logs == "1"
    assert result.cost_usd == pytest.approx(0.03)


def test_run_orchestrator_skips_cache_when_code_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["code_results"].append(  # type: ignore[union-attr]
            {"code": "print(1)", "logs": "1", "images": []}
        )
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="run some code", mode=Mode.smart))

    key = cache.make_key("run some code", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_code_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs: object):
        kwargs["code_results"].append(  # type: ignore[union-attr]
            {"code": "print(1)", "logs": "1", "images": []}
        )
        yield "note"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["code_results"] == [
        {"code": "print(1)", "logs": "1", "images": []}
    ]


def test_stream_orchestrator_omits_code_results_key_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kwargs: iter(["ok"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert "code_results" not in done["data"]


# --- HTTP: end-to-end persistence ----------------------------------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_code_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse, CodeResult

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="The answer is 4.",
            mode_used="smart",
            notes="n",
            code_results=[CodeResult(code="print(2 + 2)", logs="4", images=[])],
        )

    monkeypatch.setattr("app.main.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what is 2+2, verify with code"},
    )

    assert r.status_code == 200
    assert r.json()["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": []}
    ]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": []}
    ]


def test_stream_ask_persists_code_results_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "The answer is 4.",
                "mode_used": "smart",
                "notes": "n",
                "code_results": [{"code": "print(2 + 2)", "logs": "4", "images": []}],
            },
        }

    monkeypatch.setattr("app.main.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "what is 2+2, verify with code"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": []}
    ]
