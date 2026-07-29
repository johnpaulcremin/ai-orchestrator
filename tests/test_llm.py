from __future__ import annotations

import base64
import types

import httpx
import pytest
from openai import BadRequestError

from app import orchestrator, orchestrator_calls, providers
from app.usage import Usage


def _fake_openai(create_fn):
    responses = types.SimpleNamespace(create=create_fn)
    client = types.SimpleNamespace(responses=responses)
    client.with_options = lambda **_kw: client
    return client


def _bad_request(message: str = "bad") -> BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return BadRequestError(
        message, response=httpx.Response(400, request=request), body=None
    )


# --- OpenAI non-streaming ---------------------------------------------------


def test_call_openai_passes_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_text="ANSWER")

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))

    out = orchestrator_calls._call_openai("gpt-5", "q", 100, "low")
    assert out == "ANSWER"
    assert calls[0]["reasoning"] == {"effort": "low"}


def test_call_openai_retries_without_reasoning_on_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "reasoning" in kwargs:
            raise _bad_request()
        return types.SimpleNamespace(output_text="OK")

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))

    out = orchestrator_calls._call_openai("gpt-5", "q", 100, "high")
    assert out == "OK"
    assert len(calls) == 2
    assert "reasoning" in calls[0] and "reasoning" not in calls[1]


def test_extract_text_returns_empty_when_no_output() -> None:
    """An empty-output response (reasoning truncated, no exception) must yield ''
    — never the object's repr — so the empty-answer guards in main.py fire and a
    'Response(...)' string is never persisted as the assistant reply.
    """
    assert orchestrator._extract_text(types.SimpleNamespace(output_text="")) == ""
    assert orchestrator._extract_text(types.SimpleNamespace(output_text=None)) == ""
    # A real answer is still returned, stripped.
    assert (
        orchestrator._extract_text(types.SimpleNamespace(output_text="  hi  ")) == "hi"
    )


# --- OpenAI streaming -------------------------------------------------------


def _event(type_: str, **kw):
    return types.SimpleNamespace(type=type_, **kw)


def test_stream_openai_yields_text_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="Hel"),
                _event("response.output_text.delta", delta="lo"),
                _event("response.completed"),
            ]
        )

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    assert list(orchestrator_calls._stream_openai("gpt-5", "q", 100)) == ["Hel", "lo"]


def test_stream_openai_raises_on_failure_event(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="partial"),
                _event(
                    "response.failed",
                    response=types.SimpleNamespace(
                        error=types.SimpleNamespace(message="boom")
                    ),
                ),
            ]
        )

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    gen = orchestrator_calls._stream_openai("gpt-5", "q", 100)
    assert next(gen) == "partial"
    with pytest.raises(orchestrator_calls._ModelStreamError):
        next(gen)


def test_stream_openai_records_usage_on_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated response (reasoning consumed the budget) still reports usage.

    It must be recorded so the call isn't billed as $0, and must NOT raise — any
    partial text already streamed is kept.
    """
    incomplete = types.SimpleNamespace(
        usage=types.SimpleNamespace(
            input_tokens=500,
            output_tokens=4000,
            input_tokens_details=types.SimpleNamespace(cached_tokens=0),
        ),
        incomplete_details=types.SimpleNamespace(reason="max_output_tokens"),
    )

    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="par"),
                _event("response.incomplete", response=incomplete),
            ]
        )

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    usage = Usage()
    out = list(orchestrator_calls._stream_openai("gpt-5", "q", 100, usage=usage))

    assert out == ["par"]  # partial text kept, no raise
    assert usage.input_tokens == 500
    assert usage.output_tokens == 4000  # not silently $0-billed


# --- web_search / citations --------------------------------------------------


def _url_citation(url: str, title: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(type="url_citation", url=url, title=title or url)


def _response_with_citations(*annotations) -> types.SimpleNamespace:
    content = types.SimpleNamespace(annotations=list(annotations))
    item = types.SimpleNamespace(content=[content])
    return types.SimpleNamespace(output=[item], output_text="answer", usage=None)


def test_extract_citations_dedupes_caps_and_filters_type() -> None:
    result = _response_with_citations(
        _url_citation("https://a.example", "A"),
        _url_citation("https://a.example", "A dup"),  # same URL, dropped
        types.SimpleNamespace(type="file_citation", url="ignored"),  # wrong type
        *[_url_citation(f"https://n{i}.example") for i in range(10)],
    )
    citations = orchestrator._extract_citations(result)
    assert citations[0] == {"title": "A", "url": "https://a.example"}
    assert len(citations) == orchestrator._MAX_CITATIONS  # capped, not 11


def test_extract_citations_tolerates_missing_shape() -> None:
    assert orchestrator._extract_citations(types.SimpleNamespace()) == []
    assert orchestrator._extract_citations(object()) == []


def test_extract_citations_rejects_non_http_schemes() -> None:
    """Review follow-up: a javascript:/data: URL must never survive into
    citations — React escapes text content but not an <a href> attribute, so
    this is the single choke point (persisted history + live SSE alike) that
    has to filter it before it ever reaches the frontend.
    """
    result = _response_with_citations(
        _url_citation("javascript:alert(document.cookie)", "evil"),
        _url_citation("data:text/html,<script>evil</script>", "evil2"),
        _url_citation("https://real.example", "Real"),
        _url_citation("HTTPS://Also-Real.example", "Also real"),  # case-insensitive
    )
    citations = orchestrator._extract_citations(result)
    assert citations == [
        {"title": "Real", "url": "https://real.example"},
        {"title": "Also real", "url": "HTTPS://Also-Real.example"},
    ]


def test_call_openai_web_search_false_never_sends_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(output_text="ANSWER")

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    orchestrator_calls._call_openai("gpt-5", "q", 100)
    assert "tools" not in calls[0]


def test_call_openai_web_search_sends_tool_and_collects_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return _response_with_citations(_url_citation("https://x.example", "X"))

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    citations: list[orchestrator.Citation] = []
    out = orchestrator_calls._call_openai(
        "gpt-5", "q", 100, web_search=True, citations=citations
    )

    assert out == "answer"
    assert calls[0]["tools"] == [{"type": "web_search"}]
    assert citations == [{"title": "X", "url": "https://x.example"}]


def test_call_openai_degrades_when_web_search_tool_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that rejects the web_search tool still answers — just without a
    search — instead of failing the whole call."""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise _bad_request()
        return types.SimpleNamespace(output_text="ANSWER", output=[])

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    citations: list[orchestrator.Citation] = []
    out = orchestrator_calls._call_openai(
        "gpt-5", "q", 100, web_search=True, citations=citations
    )

    assert out == "ANSWER"
    assert citations == []
    assert all("tools" not in c for c in calls[1:])  # later attempts dropped it


def test_call_openai_reasoning_and_web_search_degrade_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning is dropped before web_search — the richest combination first,
    each BadRequest peeling off exactly one optional param."""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:  # only the tools-bearing attempts fail
            raise _bad_request()
        return types.SimpleNamespace(output_text="OK", output=[])

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    out = orchestrator_calls._call_openai("gpt-5", "q", 100, "high", web_search=True)

    assert out == "OK"
    # attempt 1: reasoning+tools (rejected), attempt 2: reasoning only (succeeds)
    assert len(calls) == 2
    assert "tools" in calls[0] and "reasoning" in calls[0]
    assert "tools" not in calls[1] and "reasoning" in calls[1]


def test_call_openai_reraises_immediately_on_non_param_badrequest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review follow-up: a BadRequest that isn't about reasoning/web_search at
    all (e.g. content moderation) must not be misattributed to a rejected
    param and retried 2-3 more times against the live API — it should surface
    on the first attempt.
    """
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        raise _bad_request("Invalid prompt: this content was flagged by moderation.")

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))

    with pytest.raises(BadRequestError):
        orchestrator_calls._call_openai("gpt-5", "q", 100, "high", web_search=True)

    assert len(calls) == 1  # no wasted retries chasing the wrong cause


def test_call_openai_still_degrades_on_ordinary_param_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The moderation short-circuit must not misfire on a genuine param
    rejection phrased without any of the non-param marker words — the ladder
    still degrades gracefully in that (the common) case."""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise _bad_request("Unsupported parameter: 'tools'.")
        return types.SimpleNamespace(output_text="OK", output=[])

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    out = orchestrator_calls._call_openai("gpt-5", "q", 100, web_search=True)

    assert out == "OK"
    assert len(calls) == 2


def test_stream_openai_web_search_collects_citations_on_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_response = _response_with_citations(_url_citation("https://c.example"))

    def create(**_kwargs):
        return iter(
            [
                _event("response.output_text.delta", delta="hi"),
                _event("response.completed", response=completed_response),
            ]
        )

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    citations: list[orchestrator.Citation] = []
    out = list(
        orchestrator_calls._stream_openai(
            "gpt-5", "q", 100, web_search=True, citations=citations
        )
    )

    assert out == ["hi"]
    assert citations == [{"title": "https://c.example", "url": "https://c.example"}]


def test_stream_openai_web_search_collects_citations_on_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete_response = _response_with_citations(_url_citation("https://d.example"))
    incomplete_response.incomplete_details = types.SimpleNamespace(reason="truncated")

    def create(**_kwargs):
        return iter([_event("response.incomplete", response=incomplete_response)])

    monkeypatch.setattr(orchestrator_calls, "get_client", lambda: _fake_openai(create))
    citations: list[orchestrator.Citation] = []
    list(
        orchestrator_calls._stream_openai(
            "gpt-5", "q", 100, web_search=True, citations=citations
        )
    )

    assert citations == [{"title": "https://d.example", "url": "https://d.example"}]


# --- timeout parsing --------------------------------------------------------


def test_timeout_seconds_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "30")
    assert orchestrator_calls._timeout_seconds() == 30.0
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "abc")
    assert orchestrator_calls._timeout_seconds() == 120.0
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "-5")
    assert orchestrator_calls._timeout_seconds() == 120.0
    monkeypatch.delenv("OPENAI_TIMEOUT_SECONDS", raising=False)
    assert orchestrator_calls._timeout_seconds() == 120.0


# --- Anthropic provider -----------------------------------------------------


def test_anthropic_model_strips_prefix() -> None:
    assert providers._anthropic_model("anthropic/claude-opus-4-8") == "claude-opus-4-8"
    assert providers._anthropic_model("claude-sonnet-5") == "claude-sonnet-5"


def test_anthropic_client_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_anthropic_client", None)
    with pytest.raises(RuntimeError):
        providers.anthropic_client(30.0)


def test_call_anthropic_joins_only_text_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="Hello "),
            types.SimpleNamespace(type="tool_use", text="IGNORED"),
            types.SimpleNamespace(type="text", text="world"),
        ]
    )
    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **_kw: message)
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    assert providers.call_anthropic("claude-x", "q", 100, 30.0) == "Hello world"


def test_stream_anthropic_yields_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        text_stream = ["a", "b", ""]

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **_kw: FakeStream())
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    assert list(providers.stream_anthropic("claude-x", "q", 100, 30.0)) == ["a", "b"]


# --- Anthropic web search (cross-provider tool parity) ----------------------


def _citation(url: str, title: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(url=url, title=title or url)


def test_extract_anthropic_citations_dedupes_caps_and_filters_scheme() -> None:
    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(
                type="text",
                text="a",
                citations=[
                    _citation("https://a.example", "A"),
                    _citation("https://a.example", "A again — a dupe"),
                    _citation("javascript:alert(1)"),
                    _citation(""),
                ],
            ),
            types.SimpleNamespace(
                type="text",
                text="b",
                citations=[_citation(f"https://{n}.example") for n in range(10)],
            ),
        ]
    )
    citations = providers._extract_anthropic_citations(message)
    assert citations[0] == {"title": "A", "url": "https://a.example"}
    assert len(citations) == providers._MAX_ANTHROPIC_CITATIONS
    assert all(c["url"].startswith("https://") for c in citations)


def test_extract_anthropic_citations_tolerates_blocks_without_any() -> None:
    message = types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="no search happened")]
    )
    assert providers._extract_anthropic_citations(message) == []


def test_call_anthropic_web_search_false_never_sends_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "q", 100, 30.0)

    assert "tools" not in captured


def test_call_anthropic_web_search_sends_tool_and_collects_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(
                    type="text",
                    text="answer",
                    citations=[_citation("https://s.example", "S")],
                )
            ]
        )

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    citations: list[dict[str, str]] = []
    out = providers.call_anthropic(
        "claude-x", "q", 100, 30.0, web_search=True, citations=citations
    )

    assert out == "answer"
    assert captured["tools"] == [providers._ANTHROPIC_WEB_SEARCH_TOOL]
    assert citations == [{"title": "S", "url": "https://s.example"}]


def test_stream_anthropic_web_search_collects_citations_from_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    final_message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(
                type="text",
                text="answer",
                citations=[_citation("https://s.example", "S")],
            )
        ],
        usage=None,
        stop_reason="end_turn",
    )

    class FakeStream:
        text_stream = ["answer"]

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return final_message

    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    citations: list[dict[str, str]] = []
    list(
        providers.stream_anthropic(
            "claude-x", "q", 100, 30.0, web_search=True, citations=citations
        )
    )

    assert captured["tools"] == [providers._ANTHROPIC_WEB_SEARCH_TOOL]
    assert citations == [{"title": "S", "url": "https://s.example"}]


# --- Anthropic action proposals (cross-provider tool parity) ----------------


def _tool_use(name: str, input_: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", name=name, input=input_)


def test_anthropic_action_tool_is_freeform_without_named_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACTIONS_WEBHOOKS", raising=False)
    tool = providers._anthropic_action_tool()
    assert tool["name"] == "propose_action"
    action_property = tool["input_schema"]["properties"]["action"]
    assert "enum" not in action_property


def test_anthropic_action_tool_restricts_to_enum_of_named_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACTIONS_WEBHOOKS", '{"send_email": "https://a", "update_sheet": "https://b"}'
    )
    tool = providers._anthropic_action_tool()
    action_property = tool["input_schema"]["properties"]["action"]
    assert action_property["enum"] == ["send_email", "update_sheet"]


def test_extract_anthropic_pending_action_valid() -> None:
    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="text", text="ok"),
            _tool_use(
                "propose_action",
                {
                    "action": "send_email",
                    "summary": "Email Bob",
                    "payload": {"to": "b"},
                },
            ),
        ]
    )
    action = providers._extract_anthropic_pending_action(message)
    assert action == {
        "action": "send_email",
        "summary": "Email Bob",
        "payload": {"to": "b"},
    }


def test_extract_anthropic_pending_action_ignores_other_tool_names() -> None:
    message = types.SimpleNamespace(
        content=[
            _tool_use("some_other_tool", {"action": "x", "summary": "y", "payload": {}})
        ]
    )
    assert providers._extract_anthropic_pending_action(message) is None


def test_extract_anthropic_pending_action_missing_fields_tolerated() -> None:
    message = types.SimpleNamespace(
        content=[
            _tool_use("propose_action", {"action": "", "summary": "y", "payload": {}})
        ]
    )
    assert providers._extract_anthropic_pending_action(message) is None


def test_extract_anthropic_pending_action_no_content_attr() -> None:
    assert providers._extract_anthropic_pending_action(object()) is None


def test_call_anthropic_actions_false_never_sends_action_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "q", 100, 30.0)

    assert "tools" not in captured


def test_call_anthropic_actions_sends_tool_and_populates_pending_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[
                _tool_use(
                    "propose_action",
                    {
                        "action": "send_email",
                        "summary": "Email Bob",
                        "payload": {"to": "b"},
                    },
                )
            ]
        )

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    pending_action: list[dict] = []
    out = providers.call_anthropic(
        "claude-x", "q", 100, 30.0, actions=True, pending_action=pending_action
    )

    assert out == ""  # no text block, only the tool call
    assert captured["tools"] == [providers._anthropic_action_tool()]
    assert pending_action == [
        {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
    ]


def test_call_anthropic_web_search_and_actions_together_send_both_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "q", 100, 30.0, web_search=True, actions=True)

    assert captured["tools"] == [
        providers._ANTHROPIC_WEB_SEARCH_TOOL,
        providers._anthropic_action_tool(),
    ]


def test_stream_anthropic_actions_populates_pending_action_from_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    final_message = types.SimpleNamespace(
        content=[
            _tool_use(
                "propose_action",
                {
                    "action": "send_email",
                    "summary": "Email Bob",
                    "payload": {"to": "b"},
                },
            )
        ],
        usage=None,
        stop_reason="tool_use",
    )

    class FakeStream:
        text_stream: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return final_message

    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    pending_action: list[dict] = []
    list(
        providers.stream_anthropic(
            "claude-x", "q", 100, 30.0, actions=True, pending_action=pending_action
        )
    )

    assert captured["tools"] == [providers._anthropic_action_tool()]
    assert pending_action == [
        {"action": "send_email", "summary": "Email Bob", "payload": {"to": "b"}}
    ]


# --- Anthropic code execution (cross-provider tool parity) ------------------


def _server_tool_use(block_id: str, code: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type="server_tool_use",
        id=block_id,
        name="code_execution",
        input={"code": code},
    )


def _code_result_block(
    tool_use_id: str,
    stdout: str = "",
    stderr: str = "",
    file_ids: list[str] | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        type="code_execution_tool_result",
        tool_use_id=tool_use_id,
        content=types.SimpleNamespace(
            type="code_execution_result",
            stdout=stdout,
            stderr=stderr,
            content=[
                types.SimpleNamespace(type="code_execution_output", file_id=fid)
                for fid in (file_ids or [])
            ],
        ),
    )


def _file_metadata(mime_type: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(mime_type=mime_type)


def _fake_files_client(
    metadata_by_id: dict[str, str], bytes_by_id: dict[str, bytes]
) -> types.SimpleNamespace:
    def retrieve_metadata(file_id, **_kw):
        return _file_metadata(metadata_by_id[file_id])

    def download(file_id, **_kw):
        return types.SimpleNamespace(read=lambda: bytes_by_id[file_id])

    return types.SimpleNamespace(
        beta=types.SimpleNamespace(
            files=types.SimpleNamespace(
                retrieve_metadata=retrieve_metadata, download=download
            )
        )
    )


def test_extract_anthropic_code_results_pairs_use_and_result_blocks() -> None:
    message = types.SimpleNamespace(
        content=[
            _server_tool_use("toolu_1", "print(1 + 1)"),
            _code_result_block("toolu_1", stdout="2\n"),
        ]
    )
    results = providers._extract_anthropic_code_results(message)
    assert results == [
        {"code": "print(1 + 1)", "logs": "2\n", "images": [], "files": []}
    ]


def test_extract_anthropic_code_results_joins_stdout_and_stderr() -> None:
    message = types.SimpleNamespace(
        content=[
            _server_tool_use("toolu_1", "1/0"),
            _code_result_block("toolu_1", stdout="", stderr="ZeroDivisionError"),
        ]
    )
    results = providers._extract_anthropic_code_results(message)
    assert results[0]["logs"] == "ZeroDivisionError"


def test_extract_anthropic_code_results_ignores_unmatched_result() -> None:
    message = types.SimpleNamespace(
        content=[_code_result_block("no_such_id", stdout="orphaned")]
    )
    assert providers._extract_anthropic_code_results(message) == []


def test_extract_anthropic_code_results_ignores_other_server_tools() -> None:
    message = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(
                type="server_tool_use", id="t1", name="web_search", input={}
            ),
            _code_result_block("t1", stdout="unrelated"),
        ]
    )
    assert providers._extract_anthropic_code_results(message) == []


def test_extract_anthropic_code_results_no_content_attr() -> None:
    assert providers._extract_anthropic_code_results(object()) == []


def test_extract_anthropic_code_results_without_client_leaves_images_empty() -> None:
    """A generated file exists (file_id present) but no client was passed —
    downloading is opt-in per call, so images must stay empty rather than
    silently attempting a download."""
    message = types.SimpleNamespace(
        content=[
            _server_tool_use("toolu_1", "plot()"),
            _code_result_block("toolu_1", stdout="", file_ids=["file_abc"]),
        ]
    )
    results = providers._extract_anthropic_code_results(message)
    assert results == [{"code": "plot()", "logs": None, "images": [], "files": []}]


def test_download_anthropic_code_file_returns_data_url_for_image() -> None:
    raw = b"\x89PNG..."
    fake_client = _fake_files_client({"file_abc": "image/png"}, {"file_abc": raw})
    kind, payload = providers._download_anthropic_code_file(fake_client, "file_abc")
    expected_b64 = base64.b64encode(raw).decode()
    assert kind == "image"
    assert payload == f"data:image/png;base64,{expected_b64}"


def test_download_anthropic_code_file_downloads_allowlisted_non_image_mime() -> None:
    raw = b"a,b\n1,2\n"
    fake_client = _fake_files_client({"file_abc": "text/csv"}, {"file_abc": raw})
    kind, payload = providers._download_anthropic_code_file(fake_client, "file_abc")
    expected_b64 = base64.b64encode(raw).decode()
    assert kind == "file"
    assert payload == {
        "filename": "file_abc",
        "mime_type": "text/csv",
        "data": f"data:text/csv;base64,{expected_b64}",
    }


def test_download_anthropic_code_file_skips_unsupported_mime() -> None:
    fake_client = _fake_files_client(
        {"file_abc": "application/x-executable"}, {"file_abc": b"\x7fELF"}
    )
    assert providers._download_anthropic_code_file(fake_client, "file_abc") is None


def test_download_anthropic_code_file_tolerates_download_failure() -> None:
    class BoomClient:
        beta = types.SimpleNamespace(
            files=types.SimpleNamespace(
                retrieve_metadata=lambda *a, **k: (_ for _ in ()).throw(
                    RuntimeError("boom")
                )
            )
        )

    assert providers._download_anthropic_code_file(BoomClient(), "file_x") is None


def test_extract_anthropic_code_results_downloads_generated_images_with_client() -> (
    None
):
    message = types.SimpleNamespace(
        content=[
            _server_tool_use("toolu_1", "plot()"),
            _code_result_block("toolu_1", stdout="", file_ids=["file_abc", "file_csv"]),
        ]
    )
    fake_client = _fake_files_client(
        {"file_abc": "image/png", "file_csv": "text/csv"},
        {"file_abc": b"PNGDATA", "file_csv": b"a,b\n1,2\n"},
    )
    results = providers._extract_anthropic_code_results(message, fake_client)
    assert results[0]["code"] == "plot()"
    # The image goes to `images`; the CSV -- a non-image but allowlisted mime
    # -- goes to `files` instead, rather than being dropped.
    assert results[0]["images"] == [
        f"data:image/png;base64,{base64.b64encode(b'PNGDATA').decode()}"
    ]
    csv_b64 = base64.b64encode(b"a,b\n1,2\n").decode()
    assert results[0]["files"] == [
        {
            "filename": "file_csv",
            "mime_type": "text/csv",
            "data": f"data:text/csv;base64,{csv_b64}",
        }
    ]


def test_call_anthropic_code_execution_false_uses_stable_client_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    def beta_create(**_kwargs):
        raise AssertionError("beta client must not be used when code_execution=False")

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=create),
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=beta_create)),
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "q", 100, 30.0)

    assert "tools" not in captured


def test_call_anthropic_code_execution_uses_beta_client_and_populates_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**_kwargs):
        raise AssertionError("stable client must not be used when code_execution=True")

    def beta_create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[
                _server_tool_use("toolu_1", "print(1 + 1)"),
                _code_result_block("toolu_1", stdout="2\n"),
            ]
        )

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=create),
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=beta_create)),
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    code_results: list[dict] = []
    out = providers.call_anthropic(
        "claude-x", "q", 100, 30.0, code_execution=True, code_results=code_results
    )

    assert out == ""  # no text block, only the tool call + result
    assert captured["tools"] == [providers._ANTHROPIC_CODE_EXECUTION_TOOL]
    assert captured["betas"] == [providers._ANTHROPIC_CODE_EXECUTION_BETA]
    assert code_results == [
        {"code": "print(1 + 1)", "logs": "2\n", "images": [], "files": []}
    ]


def test_call_anthropic_code_execution_downloads_generated_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: call_anthropic passes its own client through to
    _extract_anthropic_code_results, so a generated plot comes back as a
    ready-to-render data URL on the answer's code_results, same as OpenAI's
    inline images -- this is the file-download follow-up."""

    def beta_create(**_kwargs):
        return types.SimpleNamespace(
            content=[
                _server_tool_use("toolu_1", "plot()"),
                _code_result_block("toolu_1", stdout="", file_ids=["file_abc"]),
            ]
        )

    files_client = _fake_files_client({"file_abc": "image/png"}, {"file_abc": b"PNG"})
    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **_kw: None),
        beta=types.SimpleNamespace(
            messages=types.SimpleNamespace(create=beta_create),
            files=files_client.beta.files,
        ),
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    code_results: list[dict] = []
    providers.call_anthropic(
        "claude-x", "q", 100, 30.0, code_execution=True, code_results=code_results
    )

    assert code_results == [
        {
            "code": "plot()",
            "logs": None,
            "images": [f"data:image/png;base64,{base64.b64encode(b'PNG').decode()}"],
            "files": [],
        }
    ]


def test_stream_anthropic_code_execution_uses_beta_client_and_populates_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    final_message = types.SimpleNamespace(
        content=[
            _server_tool_use("toolu_1", "print(1 + 1)"),
            _code_result_block("toolu_1", stdout="2\n"),
        ],
        usage=None,
        stop_reason="tool_use",
    )

    class FakeStream:
        text_stream: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return final_message

    def stream(**_kwargs):
        raise AssertionError("stable client must not be used when code_execution=True")

    def beta_stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake_client = types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=stream),
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(stream=beta_stream)),
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    code_results: list[dict] = []
    list(
        providers.stream_anthropic(
            "claude-x", "q", 100, 30.0, code_execution=True, code_results=code_results
        )
    )

    assert captured["tools"] == [providers._ANTHROPIC_CODE_EXECUTION_TOOL]
    assert captured["betas"] == [providers._ANTHROPIC_CODE_EXECUTION_BETA]
    assert code_results == [
        {"code": "print(1 + 1)", "logs": "2\n", "images": [], "files": []}
    ]


# --- Anthropic math_solve (cross-provider tool parity) ----------------------


def test_anthropic_math_solve_tool_shape() -> None:
    tool = providers._anthropic_math_solve_tool()
    assert tool["name"] == "math_solve"
    assert tool["input_schema"]["required"] == ["operation", "expression"]


def test_extract_anthropic_math_call_valid() -> None:
    message = types.SimpleNamespace(
        content=[
            _tool_use(
                "math_solve",
                {"operation": "solve", "expression": "x**2 - 4", "variable": "x"},
            )
        ]
    )
    call = providers._extract_anthropic_math_call(message)
    assert call == {"operation": "solve", "expression": "x**2 - 4", "variable": "x"}


def test_extract_anthropic_math_call_defaults_variable_to_x() -> None:
    message = types.SimpleNamespace(
        content=[
            _tool_use("math_solve", {"operation": "simplify", "expression": "2+2"})
        ]
    )
    call = providers._extract_anthropic_math_call(message)
    assert call is not None
    assert call["variable"] == "x"


def test_extract_anthropic_math_call_ignores_other_tool_names() -> None:
    message = types.SimpleNamespace(
        content=[
            _tool_use("propose_action", {"action": "x", "summary": "y", "payload": {}})
        ]
    )
    assert providers._extract_anthropic_math_call(message) is None


def test_extract_anthropic_math_call_missing_fields_tolerated() -> None:
    message = types.SimpleNamespace(
        content=[_tool_use("math_solve", {"operation": "solve"})]
    )
    assert providers._extract_anthropic_math_call(message) is None


def test_extract_anthropic_math_call_no_content_attr() -> None:
    assert providers._extract_anthropic_math_call(object()) is None


def test_call_anthropic_math_solve_false_never_sends_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic("claude-x", "q", 100, 30.0)

    assert "tools" not in captured


def test_call_anthropic_math_solve_sends_tool_and_populates_math_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[
                _tool_use(
                    "math_solve",
                    {"operation": "solve", "expression": "x**2 - 4", "variable": "x"},
                )
            ]
        )

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    math_call: list[dict] = []
    out = providers.call_anthropic(
        "claude-x", "q", 100, 30.0, math_solve=True, math_call=math_call
    )

    assert out == ""  # no text block, only the tool call
    assert captured["tools"] == [providers._anthropic_math_solve_tool()]
    assert math_call == [
        {"operation": "solve", "expression": "x**2 - 4", "variable": "x"}
    ]


def test_call_anthropic_code_execution_and_math_solve_together_send_both_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def beta_create(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[])

    fake_client = types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=beta_create))
    )
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    providers.call_anthropic(
        "claude-x", "q", 100, 30.0, code_execution=True, math_solve=True
    )

    assert captured["tools"] == [
        providers._ANTHROPIC_CODE_EXECUTION_TOOL,
        providers._anthropic_math_solve_tool(),
    ]


def test_stream_anthropic_math_solve_populates_math_call_from_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    final_message = types.SimpleNamespace(
        content=[
            _tool_use(
                "math_solve",
                {"operation": "solve", "expression": "x**2 - 4", "variable": "x"},
            )
        ],
        usage=None,
        stop_reason="tool_use",
    )

    class FakeStream:
        text_stream: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get_final_message(self):
            return final_message

    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream()

    fake_client = types.SimpleNamespace(messages=types.SimpleNamespace(stream=stream))
    monkeypatch.setattr(providers, "anthropic_client", lambda _timeout: fake_client)

    math_call: list[dict] = []
    list(
        providers.stream_anthropic(
            "claude-x", "q", 100, 30.0, math_solve=True, math_call=math_call
        )
    )

    assert captured["tools"] == [providers._anthropic_math_solve_tool()]
    assert math_call == [
        {"operation": "solve", "expression": "x**2 - 4", "variable": "x"}
    ]


# --- LiteLLM provider (Gemini / Bedrock / Mistral / ...) --------------------


def test_call_litellm_passes_args_and_extracts_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="hi from gemini")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    out = providers.call_litellm("gemini/gemini-2.5-pro", "q", 128, 30.0, "low")
    assert out == "hi from gemini"
    assert captured["model"] == "gemini/gemini-2.5-pro"
    assert captured["max_tokens"] == 128
    assert captured["reasoning_effort"] == "low"
    assert captured["messages"] == [{"role": "user", "content": "q"}]


def test_call_litellm_omits_reasoning_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def completion(**kwargs):
        captured.update(kwargs)
        message = types.SimpleNamespace(content="ok")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    providers.call_litellm("mistral/mistral-large-latest", "q", 128, 30.0, "")
    assert "reasoning_effort" not in captured


def test_stream_litellm_yields_delta_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def chunk(content):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(delta=types.SimpleNamespace(content=content))
            ]
        )

    def completion(**_kwargs):
        return iter([chunk("Hel"), chunk("lo"), chunk(None)])

    monkeypatch.setattr(
        providers, "_litellm", lambda: types.SimpleNamespace(completion=completion)
    )

    assert list(providers.stream_litellm("bedrock/x", "q", 128, 30.0)) == ["Hel", "lo"]
