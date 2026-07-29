"""Code execution: the code_interpreter/code_execution tool gating/config,
OpenAI output extraction, cost estimation, the cache-skip invariant, and
end-to-end persistence.

The tool runs in a sandboxed container in the provider's own cloud, never on
this machine — same trust boundary as web_search/image_generation. Reaches
both OpenAI (code_interpreter) and Anthropic (beta code_execution) — see
tests/test_llm.py for the Anthropic-specific extraction/dispatch tests
(providers.call_anthropic/_extract_anthropic_code_results), mirroring how
Anthropic's web-search/action-tool tests live there rather than here.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

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
from app.schemas import AskRequest, CodeFile, Mode
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


# --- schemas: CodeFile -----------------------------------------------------------


def test_code_file_accepts_allowlisted_mime() -> None:
    data = "data:text/csv;base64," + base64.b64encode(b"a,b\n1,2\n").decode()
    file = CodeFile(filename="out.csv", mime_type="text/csv", data=data)
    assert file.filename == "out.csv"


def test_code_file_rejects_unsupported_mime() -> None:
    data = "data:application/zip;base64," + base64.b64encode(b"x").decode()
    with pytest.raises(ValidationError):
        CodeFile(filename="out.zip", mime_type="application/zip", data=data)


def test_code_file_rejects_malformed_data_url() -> None:
    with pytest.raises(ValidationError):
        CodeFile(filename="out.csv", mime_type="text/csv", data="not-a-data-url")


def test_code_file_rejects_oversized_file() -> None:
    huge = "data:text/csv;base64," + ("A" * 15_000_000)
    with pytest.raises(ValidationError):
        CodeFile(filename="out.csv", mime_type="text/csv", data=huge)


def test_code_file_rejects_empty_filename() -> None:
    data = "data:text/csv;base64," + base64.b64encode(b"a").decode()
    with pytest.raises(ValidationError):
        CodeFile(filename="   ", mime_type="text/csv", data=data)


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
        {"code": "print(2 + 2)", "logs": "4", "images": [], "files": []}
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
        {
            "code": "plot()",
            "logs": None,
            "images": ["https://example.com/plot.png"],
            "files": [],
        }
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


# --- orchestrator: _extract_code_results non-image file download ---------------


def _container_file_citation(container_id: str, file_id: str, filename: str) -> object:
    return SimpleNamespace(
        type="container_file_citation",
        container_id=container_id,
        file_id=file_id,
        filename=filename,
    )


def _output_text_item(annotations: list[object]) -> object:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", annotations=annotations)],
    )


def _fake_openai_files_client(bytes_by_id: dict[str, bytes]) -> object:
    def retrieve(file_id: str, container_id: str) -> object:
        return SimpleNamespace(read=lambda: bytes_by_id[file_id])

    return SimpleNamespace(
        containers=SimpleNamespace(
            files=SimpleNamespace(content=SimpleNamespace(retrieve=retrieve))
        )
    )


def test_extract_code_results_downloads_container_file_citation_with_client() -> None:
    call = _fake_code_call(
        "completed",
        "df.to_excel('out.xlsx')",
        [SimpleNamespace(type="logs", logs="saved")],
    )
    citation = _container_file_citation("cntr_1", "file_1", "out.xlsx")
    result = SimpleNamespace(output=[call, _output_text_item([citation])])
    raw = b"PK\x03\x04fakexlsx"
    client = _fake_openai_files_client({"file_1": raw})

    extracted = _extract_code_results(result, client=client)

    assert len(extracted) == 1
    expected_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert extracted[0]["files"] == [
        {
            "filename": "out.xlsx",
            "mime_type": expected_mime,
            "data": f"data:{expected_mime};base64,{base64.b64encode(raw).decode()}",
        }
    ]


def test_extract_code_results_leaves_files_empty_without_client() -> None:
    """A file citation exists but no client was passed -- downloading is
    opt-in per call, same as the Anthropic path (see test_llm.py)."""
    call = _fake_code_call("completed", "code", [])
    citation = _container_file_citation("cntr_1", "file_1", "out.xlsx")
    result = SimpleNamespace(output=[call, _output_text_item([citation])])

    extracted = _extract_code_results(result)

    assert extracted[0]["files"] == []


def test_extract_code_results_skips_unsupported_mime_citation() -> None:
    call = _fake_code_call("completed", "code", [])
    citation = _container_file_citation("cntr_1", "file_1", "out.exe")
    result = SimpleNamespace(output=[call, _output_text_item([citation])])
    client = _fake_openai_files_client({"file_1": b"MZ..."})

    extracted = _extract_code_results(result, client=client)

    assert extracted[0]["files"] == []


def test_extract_code_results_skips_oversized_file() -> None:
    call = _fake_code_call("completed", "code", [])
    citation = _container_file_citation("cntr_1", "file_1", "out.xlsx")
    result = SimpleNamespace(output=[call, _output_text_item([citation])])
    client = _fake_openai_files_client({"file_1": b"x" * (11_000_000)})

    extracted = _extract_code_results(result, client=client)

    assert extracted[0]["files"] == []


def test_extract_code_results_no_files_attached_without_any_code_call() -> None:
    """A citation with no matching code_interpreter_call has nothing to
    attach to -- dropped rather than raising."""
    citation = _container_file_citation("cntr_1", "file_1", "out.xlsx")
    result = SimpleNamespace(output=[_output_text_item([citation])])
    client = _fake_openai_files_client({"file_1": b"data"})

    assert _extract_code_results(result, client=client) == []


# --- orchestrator: note text ----------------------------------------------------


def test_code_execution_note_singular_and_plural() -> None:
    assert "a snippet" in _code_execution_note(1)
    assert "1 snippet" not in _code_execution_note(1)
    assert "2 snippets" in _code_execution_note(2)


# --- orchestrator: gating + cost + cache-skip + response wiring ---------------


def test_run_orchestrator_passes_code_execution_true_when_enabled_for_openai(
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


def test_run_orchestrator_passes_code_execution_true_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross-provider tool parity (see providers._ANTHROPIC_CODE_EXECUTION_TOOL):
    # a Claude-served tier now gets code_execution too, same as OpenAI.
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["code_execution"] = kwargs["code_execution"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.answer == "ok"
    assert seen["code_execution"] is True


def test_run_orchestrator_code_execution_false_for_litellm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LiteLLM-routed providers (Gemini, Bedrock, ...) get no hosted-tool
    # support wired up at all — code_execution never reaches call_litellm.
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["code_execution"] = kwargs["code_execution"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.answer == "ok"
    assert seen["code_execution"] is False


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

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "what is 2+2, verify with code"},
    )

    assert r.status_code == 200
    assert r.json()["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": [], "files": None}
    ]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": [], "files": None}
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

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "what is 2+2, verify with code"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["code_results"] == [
        {"code": "print(2 + 2)", "logs": "4", "images": [], "files": None}
    ]
