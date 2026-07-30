from __future__ import annotations

import pytest

from app import orchestrator, orchestrator_calls, providers
from app.schemas import AskRequest, Mode


def test_provider_of_classifies_by_model_name() -> None:
    assert providers.provider_of("gpt-5") == "openai"
    assert providers.provider_of("gpt-5-mini") == "openai"
    assert providers.provider_of("claude-sonnet-5") == "anthropic"
    assert providers.provider_of("CLAUDE-opus-4-8") == "anthropic"
    assert providers.provider_of("anthropic/claude-opus-4-8") == "anthropic"
    # Provider-prefixed names route through LiteLLM.
    assert providers.provider_of("gemini/gemini-2.5-pro") == "litellm"
    assert providers.provider_of("bedrock/anthropic.claude-3-5-sonnet") == "litellm"
    assert providers.provider_of("mistral/mistral-large-latest") == "litellm"
    assert providers.provider_of("groq/llama-3.3-70b") == "litellm"


def test_key_env_for_names_the_right_credential() -> None:
    assert providers.key_env_for("gpt-5") == "OPENAI_API_KEY"
    assert providers.key_env_for("claude-sonnet-5") == "ANTHROPIC_API_KEY"
    assert providers.key_env_for("gemini/gemini-2.5-pro") == "GEMINI_API_KEY"
    assert providers.key_env_for("mistral/mistral-large-latest") == "MISTRAL_API_KEY"
    assert providers.key_env_for("bedrock/anthropic.claude") == "AWS credentials"
    assert "somenew" in providers.key_env_for("somenew/model")
    # Ollama has no credential at all — the message must point at the local
    # server, not a nonexistent OLLAMA_API_KEY.
    assert "Ollama server" in providers.key_env_for("ollama/llama3.1:8b")


def test_call_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator_calls,
        "call_anthropic",
        lambda model, q, mt, to, usage=None, attachments=None, files=None, truncated=None, system=None, web_search=False, citations=None, actions=False, pending_action=None, code_execution=False, code_results=None, math_solve=False, math_call=None, capabilities=False, capabilities_call=None: (
            f"claude:{model}"
        ),
    )
    monkeypatch.setattr(
        orchestrator_calls,
        "call_litellm",
        lambda model, q, mt, to, re="", usage=None, attachments=None, files=None, truncated=None, system=None: (
            f"litellm:{model}"
        ),
    )
    monkeypatch.setattr(
        orchestrator_calls, "_call_openai", lambda *a, **k: "openai-answer"
    )

    assert (
        orchestrator_calls._call_model("claude-sonnet-5", "hi", 100)
        == "claude:claude-sonnet-5"
    )
    assert orchestrator_calls._call_model("gpt-5", "hi", 100) == "openai-answer"
    assert (
        orchestrator_calls._call_model("gemini/gemini-2.5-pro", "hi", 100)
        == "litellm:gemini/gemini-2.5-pro"
    )


def test_call_model_forwards_web_search_and_citations_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider tool parity: web_search/citations now reach the
    Anthropic branch too (see providers.call_anthropic's own web_search
    param); images stay OpenAI-only (no Anthropic/LiteLLM equivalent wired
    up). See test_call_model_forwards_actions_and_composes_the_confirmation_note
    and test_call_model_forwards_code_execution_and_composes_the_note for the
    actions/pending_action and code_execution/code_results equivalents."""
    captured: dict = {}

    def fake_call_anthropic(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        captured["web_search"] = web_search
        citations.append({"title": "T", "url": "https://s.example"})
        return "answer"

    monkeypatch.setattr(orchestrator_calls, "call_anthropic", fake_call_anthropic)

    citations: list[dict[str, str]] = []
    result = orchestrator_calls._call_model(
        "claude-sonnet-5", "hi", 100, web_search=True, citations=citations
    )

    assert result == "answer"
    assert captured["web_search"] is True
    assert citations == [{"title": "T", "url": "https://s.example"}]


def test_call_model_forwards_actions_and_composes_the_confirmation_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider tool parity: propose_action now reaches the Anthropic
    branch too, and _call_model composes the same confirmation note into the
    answer text OpenAI's own _call_openai already does -- Claude's tool_use
    carries no text, so this is the ONLY source of that note for Claude."""
    captured: dict = {}

    def fake_call_anthropic(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        captured["actions"] = actions
        pending_action.append(
            {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
        )
        return ""  # Claude's tool-only response carries no text.

    monkeypatch.setattr(orchestrator_calls, "call_anthropic", fake_call_anthropic)

    pending_action: list[dict] = []
    result = orchestrator_calls._call_model(
        "claude-sonnet-5", "hi", 100, actions=True, pending_action=pending_action
    )

    assert captured["actions"] is True
    assert pending_action == [
        {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
    ]
    assert "Email Bob" in result
    assert "Confirm below to run it" in result


def test_stream_model_forwards_actions_and_yields_the_confirmation_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream_anthropic(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        yield "here you go"
        pending_action.append(
            {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
        )

    monkeypatch.setattr(orchestrator_calls, "stream_anthropic", fake_stream_anthropic)

    pending_action: list[dict] = []
    chunks = list(
        orchestrator_calls._stream_model(
            "claude-sonnet-5", "hi", 100, actions=True, pending_action=pending_action
        )
    )

    assert chunks[0] == "here you go"
    # A second chunk exists ONLY once the stream has finished (pending_action
    # is populated at the end of fake_stream_anthropic's generator), and is
    # prefixed with a blank line since real text already streamed.
    assert chunks[1].startswith("\n\n")
    assert "Email Bob" in chunks[1]
    assert pending_action == [
        {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
    ]


def test_call_model_forwards_code_execution_and_composes_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-provider tool parity: code_execution now reaches the Anthropic
    branch too, and _call_model composes the same "ran a snippet" note into
    the answer text OpenAI's own _call_openai already does -- Claude's
    server_tool_use carries no text of its own, same asymmetry as actions."""
    captured: dict = {}

    def fake_call_anthropic(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        captured["code_execution"] = code_execution
        code_results.append({"code": "print(1)", "logs": "1", "images": []})
        return ""  # Claude's tool-only response carries no text.

    monkeypatch.setattr(orchestrator_calls, "call_anthropic", fake_call_anthropic)

    code_results: list[dict] = []
    result = orchestrator_calls._call_model(
        "claude-sonnet-5", "hi", 100, code_execution=True, code_results=code_results
    )

    assert captured["code_execution"] is True
    assert code_results == [{"code": "print(1)", "logs": "1", "images": []}]
    assert "Ran a snippet of code" in result


def test_stream_model_forwards_code_execution_and_yields_the_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_stream_anthropic(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        yield "here you go"
        code_results.append({"code": "print(1)", "logs": "1", "images": []})

    monkeypatch.setattr(orchestrator_calls, "stream_anthropic", fake_stream_anthropic)

    code_results: list[dict] = []
    chunks = list(
        orchestrator_calls._stream_model(
            "claude-sonnet-5",
            "hi",
            100,
            code_execution=True,
            code_results=code_results,
        )
    )

    assert chunks[0] == "here you go"
    assert chunks[1].startswith("\n\n")
    assert "Ran a snippet of code" in chunks[1]
    assert code_results == [{"code": "print(1)", "logs": "1", "images": []}]


def test_stream_model_dispatches_by_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator_calls,
        "stream_anthropic",
        lambda model, q, mt, to, usage=None, attachments=None, files=None, truncated=None, system=None, web_search=False, citations=None, actions=False, pending_action=None, code_execution=False, code_results=None, math_solve=False, math_call=None, capabilities=False, capabilities_call=None: (
            iter(["a", "b"])
        ),
    )
    monkeypatch.setattr(
        orchestrator_calls,
        "stream_litellm",
        lambda model, q, mt, to, re="", usage=None, attachments=None, files=None, truncated=None, system=None: (
            iter(["g1", "g2"])
        ),
    )
    monkeypatch.setattr(
        orchestrator_calls, "_stream_openai", lambda *a, **k: iter(["x"])
    )

    assert list(orchestrator_calls._stream_model("claude-x", "hi", 100)) == ["a", "b"]
    assert list(orchestrator_calls._stream_model("gpt-5", "hi", 100)) == ["x"]
    assert list(orchestrator_calls._stream_model("mistral/large", "hi", 100)) == [
        "g1",
        "g2",
    ]


def test_run_orchestrator_answers_with_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator_calls,
        "call_anthropic",
        lambda model, q, mt, to, usage=None, attachments=None, files=None, truncated=None, system=None, web_search=False, citations=None, actions=False, pending_action=None, code_execution=False, code_results=None, math_solve=False, math_call=None, capabilities=False, capabilities_call=None: (
            "Bonjour"
        ),
    )

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))

    assert result.answer == "Bonjour"
    assert result.mode_used == "smart"


def test_auth_key_env_picks_provider() -> None:
    assert orchestrator_calls._auth_key_env("claude-sonnet-5") == "ANTHROPIC_API_KEY"
    assert orchestrator_calls._auth_key_env("gpt-5") == "OPENAI_API_KEY"


def test_claude_auth_error_names_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from openai import AuthenticationError

    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    response = httpx.Response(401, request=httpx.Request("POST", "https://api"))

    def boom(
        model,
        q,
        mt,
        to,
        usage=None,
        attachments=None,
        files=None,
        truncated=None,
        system=None,
        web_search=False,
        citations=None,
        actions=False,
        pending_action=None,
        code_execution=False,
        code_results=None,
        math_solve=False,
        math_call=None,
        capabilities=False,
        capabilities_call=None,
    ):
        raise AuthenticationError("bad key", response=response, body=None)

    monkeypatch.setattr(orchestrator_calls, "call_anthropic", boom)

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))
    assert result.answer == ""
    assert "ANTHROPIC_API_KEY" in result.notes


def test_non_api_error_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Claude primary that raises a plain RuntimeError (e.g. missing key at
    # client init) must still fall back to the OpenAI model — matching the
    # streaming path.
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def claude_boom(model, q, mt, to, *args, **kwargs):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(orchestrator_calls, "call_anthropic", claude_boom)
    monkeypatch.setattr(orchestrator_calls, "_call_openai", lambda *a, **k: "recovered")

    result = orchestrator.run_orchestrator(AskRequest(question="x", mode=Mode.smart))
    assert result.answer == "recovered"
    assert result.mode_used.endswith("->fallback")
