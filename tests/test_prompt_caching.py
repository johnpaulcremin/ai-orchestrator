"""Provider-native prompt caching: the stable system-prompt/history-summary
prefix (see main.build_context_prompt_with_cache_split) is threaded to
Anthropic's native `system` param with a cache_control breakpoint, instead of
being baked into the user turn on every call like everything else here.

Distinct from the app's own response cache (app/cache.py, a full-answer
cache keyed by question+mode) and context summarization (app/context_summary.py,
folding old turns into a memory summary) — this is about the PROVIDER billing
repeated tokens at a discount, not the app skipping a model call entirely.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app.providers import _anthropic_system


# --- providers._anthropic_system ----------------------------------------------


def test_anthropic_system_none_for_empty_input() -> None:
    assert _anthropic_system(None) is None
    assert _anthropic_system("") is None


def test_anthropic_system_marks_the_block_cacheable() -> None:
    block = _anthropic_system("Be terse.")
    assert block == [
        {"type": "text", "text": "Be terse.", "cache_control": {"type": "ephemeral"}}
    ]


# --- _call_model / _stream_model: anthropic_question routing -----------------


def test_call_model_sends_anthropic_question_when_cacheable_system_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ):
        captured["question"] = q
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(orchestrator, "call_anthropic", fake_call_anthropic)

    orchestrator._call_model(
        "claude-sonnet-5",
        "FULL PROMPT (system + history + question)",
        100,
        cacheable_system="STABLE SYSTEM BLOCK",
        anthropic_question="JUST THE NEW TURN",
    )

    assert captured["question"] == "JUST THE NEW TURN"
    assert captured["system"] == "STABLE SYSTEM BLOCK"


def test_call_model_falls_back_to_question_without_cacheable_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    ):
        captured["question"] = q
        return "ok"

    monkeypatch.setattr(orchestrator, "call_anthropic", fake_call_anthropic)

    orchestrator._call_model("claude-sonnet-5", "FULL PROMPT", 100)

    assert captured["question"] == "FULL PROMPT"


def test_call_model_falls_back_to_question_if_anthropic_question_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: cacheable_system without a matching anthropic_question must
    never send None as the user turn."""
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
    ):
        captured["question"] = q
        return "ok"

    monkeypatch.setattr(orchestrator, "call_anthropic", fake_call_anthropic)

    orchestrator._call_model(
        "claude-sonnet-5", "FULL PROMPT", 100, cacheable_system="STABLE SYSTEM"
    )

    assert captured["question"] == "FULL PROMPT"


def test_stream_model_sends_anthropic_question_when_cacheable_system_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

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
    ):
        captured["question"] = q
        captured["system"] = system
        yield "ok"

    monkeypatch.setattr(orchestrator, "stream_anthropic", fake_stream_anthropic)

    list(
        orchestrator._stream_model(
            "claude-sonnet-5",
            "FULL PROMPT",
            100,
            cacheable_system="STABLE SYSTEM BLOCK",
            anthropic_question="JUST THE NEW TURN",
        )
    )

    assert captured["question"] == "JUST THE NEW TURN"
    assert captured["system"] == "STABLE SYSTEM BLOCK"


# --- end-to-end: the ask endpoint never double-bills the system block --------


def _create(client: TestClient, title: str = "t") -> int:
    return int(client.post("/v1/conversations", json={"title": title}).json()["id"])


def test_ask_endpoint_never_sends_the_instructions_text_twice_to_anthropic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

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
    ):
        captured["question"] = q
        captured["system"] = system
        return "Bonjour"

    monkeypatch.setattr(orchestrator, "call_anthropic", fake_call_anthropic)

    cid = _create(client)
    client.put(
        f"/v1/conversations/{cid}/system_prompt",
        json={"system_prompt": "Always answer in French."},
    )

    res = client.post(f"/v1/conversations/{cid}/ask", json={"question": "hi"})
    assert res.status_code == 200

    # The instructions text was sent via `system` (cache_control-marked)...
    assert captured["system"] is not None
    assert "Always answer in French." in str(captured["system"])
    # ...and must NOT also appear in the user-turn `question` — sending it in
    # both places would defeat the whole point (bill it once, at full price,
    # AND again via the "cached" channel, i.e. worse than not caching at all).
    assert "Always answer in French." not in captured["question"]
    assert "hi" in captured["question"]
