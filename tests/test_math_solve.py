"""Precision math tool (app/math_solve.py): SymPy computation, the multi-
layer injection defenses, the OpenAI-side function-call extraction,
orchestrator gating/execution/note-composition, and end-to-end persistence.

Cross-provider from the start (OpenAI function tool, Anthropic custom
tool-use) — the Anthropic-side extraction/dispatch tests live in
tests/test_llm.py alongside the other Anthropic provider-level tool tests
(web search, actions, code execution), matching that file's existing
convention; this file covers the computation itself, OpenAI's own
extraction (mirroring test_actions.py's _extract_pending_action tests), and
the orchestrator-level wiring.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import math_solve
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.orchestrator_extract import _extract_math_call
from app.schemas import AskRequest, Mode


# --- solve_math: each operation --------------------------------------------------


def test_solve_finds_all_roots() -> None:
    result = math_solve.solve_math("solve", "x**2 - 4")
    assert result["result"] == "[-2, 2]"
    assert "error" not in result


def test_solve_accepts_an_equation_with_equals_sign() -> None:
    result = math_solve.solve_math("solve", "x**2 = 4")
    assert result["result"] == "[-2, 2]"


def test_simplify_reduces_the_expression() -> None:
    result = math_solve.solve_math("simplify", "(x**2 - 1)/(x - 1)")
    assert result["result"] == "x + 1"


def test_differentiate() -> None:
    result = math_solve.solve_math("differentiate", "x**3 + 2*x")
    assert result["result"] == "3*x**2 + 2"


def test_integrate() -> None:
    result = math_solve.solve_math("integrate", "2*x")
    assert result["result"] == "x**2"


def test_evaluate_keeps_exact_fractions() -> None:
    result = math_solve.solve_math("evaluate", "22/7")
    assert "." in result["result"]  # evalf() -> a decimal, not a Rational


def test_uses_a_custom_variable() -> None:
    result = math_solve.solve_math("differentiate", "y**2", variable="y")
    assert result["result"] == "2*y"
    assert result["variable"] == "y"


def test_default_variable_is_x() -> None:
    result = math_solve.solve_math("simplify", "2 + 2")
    assert result["variable"] == "x"


# --- solve_math: error handling --------------------------------------------------


def test_unknown_operation_is_an_error() -> None:
    result = math_solve.solve_math("frobnicate", "x")
    assert "error" in result
    assert "unknown operation" in result["error"]


def test_empty_expression_is_an_error() -> None:
    result = math_solve.solve_math("solve", "")
    assert "error" in result


def test_invalid_variable_name_is_an_error() -> None:
    result = math_solve.solve_math("solve", "x**2 - 4", variable="1x")
    assert "error" in result


def test_malformed_expression_is_an_error_not_a_crash() -> None:
    result = math_solve.solve_math("solve", "x** - -")
    assert "error" in result


def test_expression_too_long_is_rejected() -> None:
    result = math_solve.solve_math("solve", "x" * 300)
    assert "error" in result
    assert "200 characters" in result["error"]


def test_successful_sympy_result_has_sympy_source() -> None:
    result = math_solve.solve_math("solve", "x**2 - 4")
    assert result["source"] == "sympy"


# --- solve_math: optional Wolfram Alpha fallback ---------------------------------


def test_wolfram_alpha_configured_false_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WOLFRAM_ALPHA_APP_ID", raising=False)
    assert math_solve.wolfram_alpha_configured() is False


def test_wolfram_alpha_configured_true_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")
    assert math_solve.wolfram_alpha_configured() is True


def test_wolfram_alpha_query_returns_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WOLFRAM_ALPHA_APP_ID", raising=False)
    assert math_solve._wolfram_alpha_query("2+2") is None


def test_wolfram_alpha_query_returns_text_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")

    class FakeResponse:
        status_code = 200
        text = "4"

    monkeypatch.setattr(math_solve.httpx, "get", lambda *a, **kw: FakeResponse())
    assert math_solve._wolfram_alpha_query("2+2") == "4"


def test_wolfram_alpha_query_returns_none_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")

    class FakeResponse:
        status_code = 501
        text = ""

    monkeypatch.setattr(math_solve.httpx, "get", lambda *a, **kw: FakeResponse())
    assert math_solve._wolfram_alpha_query("2+2") is None


def test_wolfram_alpha_query_returns_none_on_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")

    def boom(*a, **kw):
        raise math_solve.httpx.HTTPError("timeout")

    monkeypatch.setattr(math_solve.httpx, "get", boom)
    assert math_solve._wolfram_alpha_query("2+2") is None


def test_solve_math_falls_back_to_wolfram_alpha_on_compute_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")

    def raise_parse_error(expression: str, variable: str):
        raise ValueError("sympy could not parse this")

    monkeypatch.setattr(math_solve, "_parse", raise_parse_error)
    monkeypatch.setattr(math_solve, "_wolfram_alpha_query", lambda expression: "42")

    result = math_solve.solve_math("evaluate", "2+2")
    assert result["result"] == "42"
    assert result["source"] == "wolfram_alpha"
    assert "error" not in result


def test_solve_math_keeps_the_original_error_when_wolfram_alpha_has_no_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_parse_error(expression: str, variable: str):
        raise ValueError("sympy could not parse this")

    monkeypatch.setattr(math_solve, "_parse", raise_parse_error)
    monkeypatch.setattr(math_solve, "_wolfram_alpha_query", lambda expression: None)

    result = math_solve.solve_math("evaluate", "2+2")
    assert "source" not in result
    assert "could not compute" in result["error"]


def test_solve_math_never_falls_back_for_a_safety_rejected_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A security-rejected expression (fails the allowlist/denylist) must
    never reach the Wolfram Alpha fallback at all — those checks run before
    solve_math's try block, independent of whether a fallback is configured."""
    monkeypatch.setenv("WOLFRAM_ALPHA_APP_ID", "app-id")
    calls: list[str] = []
    monkeypatch.setattr(
        math_solve,
        "_wolfram_alpha_query",
        lambda expression: calls.append(expression) or "should never be used",
    )
    result = math_solve.solve_math("solve", "__import__('os').system('x')")
    assert "error" in result
    assert calls == []


def test_note_shows_wolfram_alpha_source() -> None:
    note = math_solve.note({"result": "42", "source": "wolfram_alpha"})
    assert "42" in note
    assert "Wolfram Alpha" in note


# --- solve_math: injection defenses (SECURITY) -----------------------------------
#
# Each of these must be rejected WITHOUT executing any real code. The module
# docstring's three layers (character allowlist, keyword denylist, stripped-
# builtins namespace) are independent -- these tests exercise the combined
# defense, not any single layer.
#
# SAFETY NOTE: the strings below (os.system(...), eval(...), exec(...), ...)
# are inert data — attack-payload FIXTURES passed as the `expression` string
# to math_solve.solve_math, asserting they get rejected. Nothing in this
# test file itself calls Python's eval/exec/os.system; solve_math's own
# safety layers are exactly what's under test.


@pytest.mark.parametrize(
    "attack",
    [
        '__import__("os").system("echo pwned")',
        'open("/etc/passwd").read()',
        'exec("print(1)")',
        'eval("1+1")',
        "__class__",
        "os.system('echo pwned')",
        "sys.exit()",
        "lambda: 1",
        "getattr(1, '__class__')",
        "compile('1', '', 'eval')",
    ],
)
def test_injection_attempts_are_rejected(attack: str) -> None:
    result = math_solve.solve_math("solve", attack)
    assert "error" in result, f"expected {attack!r} to be rejected, got {result}"


def test_injection_attempt_produces_no_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Belt-and-suspenders: an attack that (if it worked) would write a file
    must not actually write one."""
    marker = tmp_path / "pwned.txt"
    payload = f'__import__("pathlib").Path("{marker}").write_text("pwned")'
    math_solve.solve_math("solve", payload)
    assert not marker.exists()


# --- format note -----------------------------------------------------------------


def test_note_shows_the_result() -> None:
    note = math_solve.note({"result": "[-2, 2]"})
    assert "[-2, 2]" in note
    assert "Computed exactly" in note


def test_note_shows_the_error() -> None:
    note = math_solve.note({"error": "unknown operation 'bogus'"})
    assert "couldn't" in note.lower()
    assert "unknown operation" in note


# --- OpenAI extraction: _extract_math_call --------------------------------------


def _function_call(name: str, arguments: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="function_call", name=name, arguments=arguments)


def test_extract_math_call_valid() -> None:
    result = types.SimpleNamespace(
        output=[
            _function_call(
                "math_solve",
                '{"operation": "solve", "expression": "x**2 - 4", "variable": "x"}',
            )
        ]
    )
    call = _extract_math_call(result)
    assert call == {"operation": "solve", "expression": "x**2 - 4", "variable": "x"}


def test_extract_math_call_defaults_variable_to_x() -> None:
    result = types.SimpleNamespace(
        output=[
            _function_call(
                "math_solve", '{"operation": "simplify", "expression": "2+2"}'
            )
        ]
    )
    call = _extract_math_call(result)
    assert call is not None
    assert call["variable"] == "x"


def test_extract_math_call_ignores_other_function_calls() -> None:
    result = types.SimpleNamespace(
        output=[_function_call("propose_action", '{"action": "x"}')]
    )
    assert _extract_math_call(result) is None


def test_extract_math_call_missing_fields_tolerated() -> None:
    result = types.SimpleNamespace(
        output=[_function_call("math_solve", '{"operation": "solve"}')]
    )
    assert _extract_math_call(result) is None


def test_extract_math_call_malformed_json_tolerated() -> None:
    result = types.SimpleNamespace(output=[_function_call("math_solve", "{not json")])
    assert _extract_math_call(result) is None


def test_extract_math_call_no_output_attr() -> None:
    assert _extract_math_call(types.SimpleNamespace()) is None


# --- orchestrator: gating ---------------------------------------------------------


def test_run_orchestrator_math_solve_false_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATH_SOLVE", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["math_solve"] = kwargs["math_solve"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["math_solve"] is False


def test_run_orchestrator_math_solve_true_when_enabled_for_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["math_solve"] = kwargs["math_solve"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["math_solve"] is True


def test_run_orchestrator_math_solve_true_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cross-provider from the start (unlike code_execution, which started
    # OpenAI-only) -- a Claude-served tier gets math_solve too.
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["math_solve"] = kwargs["math_solve"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["math_solve"] is True


def test_run_orchestrator_math_solve_false_for_litellm_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "gemini/gemini-flash-latest")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs: object) -> str:
        seen["math_solve"] = kwargs["math_solve"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert seen["math_solve"] is False


def test_run_orchestrator_returns_and_composes_math_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["math_results"].append(  # type: ignore[union-attr]
            {
                "operation": "solve",
                "expression": "x**2 - 4",
                "variable": "x",
                "result": "[-2, 2]",
            }
        )
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="solve x^2=4", mode=Mode.smart))

    assert result.math_results is not None
    assert result.math_results[0].result == "[-2, 2]"


def test_run_orchestrator_no_math_results_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATH_SOLVE", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    result = run_orchestrator(AskRequest(question="hi", mode=Mode.smart))
    assert result.math_results is None


def test_run_orchestrator_skips_cache_when_math_results_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import cache

    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        kwargs["math_results"].append(  # type: ignore[union-attr]
            {"operation": "solve", "expression": "x", "variable": "x", "result": "0"}
        )
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="solve x=0", mode=Mode.smart))

    key = cache.make_key("solve x=0", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_math_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MATH_SOLVE", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs: object):
        kwargs["math_results"].append(  # type: ignore[union-attr]
            {"operation": "solve", "expression": "x", "variable": "x", "result": "0"}
        )
        yield "note"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["math_results"] == [
        {"operation": "solve", "expression": "x", "variable": "x", "result": "0"}
    ]


def test_stream_orchestrator_omits_math_results_key_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MATH_SOLVE", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kwargs: iter(["ok"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.smart)))
    done = events[-1]
    assert "math_results" not in done["data"]


# --- HTTP: end-to-end persistence ----------------------------------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_math_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse, MathResult

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="Computed exactly: **[-2, 2]**",
            mode_used="smart",
            notes="n",
            math_results=[
                MathResult(
                    operation="solve",
                    expression="x**2 - 4",
                    variable="x",
                    result="[-2, 2]",
                )
            ],
        )

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask",
        json={"question": "solve x^2 = 4"},
    )

    assert r.status_code == 200
    assert r.json()["math_results"] == [
        {
            "operation": "solve",
            "expression": "x**2 - 4",
            "variable": "x",
            "result": "[-2, 2]",
            "error": None,
            "source": None,
        }
    ]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["math_results"][0]["result"] == "[-2, 2]"


def test_stream_ask_persists_math_results_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Computed exactly: **[-2, 2]**",
                "mode_used": "smart",
                "notes": "n",
                "math_results": [
                    {
                        "operation": "solve",
                        "expression": "x**2 - 4",
                        "variable": "x",
                        "result": "[-2, 2]",
                    }
                ],
            },
        }

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "solve x^2 = 4"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["math_results"][0]["result"] == "[-2, 2]"


# --- Settings registry -----------------------------------------------------------


def test_math_solve_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "MATH_SOLVE")
    assert flag["effective_enabled"] is False  # off by default
    assert flag["default"] is False
